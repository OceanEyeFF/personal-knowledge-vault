"""P1-B public CLI blackbox: Topic → Evidence → Claim → approved brief."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.storage.coordinator import StorageCoordinator
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import prepare_offline_child_env

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _seed_source(data_root: Path) -> int:
    """Create a test-owned source entry before exercising the public CLI."""

    db_path = data_root / "db" / "knowledge_vault.db"
    store = SQLiteStore(db_path)
    store.initialize()
    coordinator = StorageCoordinator(
        MarkdownStore(data_root / "vault"),
        store,
        data_root / "runtime" / "operations",
    )
    archived = coordinator.archive(
        Entry(
            title="CLI outcome source",
            source_type="text",
            content="cli-source-body-must-not-leak",
            tags=["outcome"],
            keywords=["traceability"],
        ),
        chunks=["cli-source-body-must-not-leak"],
    )
    assert archived.knowledge_id is not None
    return archived.knowledge_id


def _run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"),
            "cli",
            *args,
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )


def _source_markdown_path(data_root: Path, knowledge_id: int) -> Path:
    row = SQLiteStore(data_root / "db" / "knowledge_vault.db").query_by_id(knowledge_id)
    assert isinstance(row, dict)
    relative_path = row["file_path"]
    assert isinstance(relative_path, str)
    path = data_root / "vault" / relative_path
    assert path.is_file()
    return path


def _json_stdout(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload["status"] == "success"
    return payload


def _json_stderr(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 1
    payload = json.loads(result.stderr)
    assert isinstance(payload, dict)
    assert payload["status"] == "error"
    return payload


def test_outcome_cli_is_public_cross_process_and_body_free(tmp_path: Path) -> None:
    data_root = tmp_path / "outcome-cli"
    knowledge_id = _seed_source(data_root)
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
    env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"

    # Mutable authored text must never turn the public Outcome surface into a
    # local-path or credential echo channel.
    private_title = r"C:\private-outcome-canary\brief.md"
    unsafe_topic = _json_stderr(
        _run_cli(
            env,
            "outcome",
            "create-topic",
            "--title",
            private_title,
            "--question",
            "Does unsafe text enter a public outcome?",
        )
    )
    assert unsafe_topic["issues"][0]["code"] == "outcome_invalid"
    assert private_title not in json.dumps(unsafe_topic, ensure_ascii=False)

    topic_payload = _json_stdout(
        _run_cli(
            env,
            "outcome",
            "create-topic",
            "--title",
            "CLI public outcome",
            "--question",
            "Can the selected source support the claim?",
        )
    )
    topic = topic_payload["result"]
    assert isinstance(topic, dict)
    topic_id = topic["topic_id"]
    assert isinstance(topic_id, str)

    # A malformed source locator must fail through the public JSON envelope
    # without reflecting credentials or the untrusted input value.
    secret_locator = "https://reader:token-do-not-echo@example.invalid/source"
    rejected = _json_stderr(
        _run_cli(env, "outcome", "attach-evidence", topic_id, secret_locator)
    )
    assert rejected["issues"][0]["code"] == "outcome_invalid"
    assert "token-do-not-echo" not in json.dumps(rejected, ensure_ascii=False)
    assert secret_locator not in json.dumps(rejected, ensure_ascii=False)

    evidence_payload = _json_stdout(
        _run_cli(
            env,
            "outcome",
            "attach-evidence",
            topic_id,
            f"pkv://entries/{knowledge_id}",
        )
    )
    evidence = evidence_payload["result"]
    assert isinstance(evidence, dict)
    evidence_id = evidence["evidence_id"]
    assert isinstance(evidence_id, str)

    claim_payload = _json_stdout(
        _run_cli(
            env,
            "outcome",
            "create-claim",
            topic_id,
            "The selected source supports the scoped claim.",
            "--state",
            "supported",
        )
    )
    claim = claim_payload["result"]
    assert isinstance(claim, dict)
    claim_id = claim["claim_id"]
    assert isinstance(claim_id, str)

    _json_stdout(
        _run_cli(
            env,
            "outcome",
            "link-evidence",
            claim_id,
            evidence_id,
            "--polarity",
            "support",
        )
    )
    brief_payload = _json_stdout(_run_cli(env, "outcome", "render", topic_id))
    brief = brief_payload["result"]
    assert isinstance(brief, dict)
    assert brief["state"] == "awaiting_review"
    assert isinstance(brief["markdown_relative_path"], str)
    assert not str(brief["markdown_relative_path"]).startswith("/")
    assert str(data_root) not in json.dumps(brief_payload, ensure_ascii=False)
    assert "cli-source-body-must-not-leak" not in json.dumps(
        brief_payload, ensure_ascii=False
    )

    approved_payload = _json_stdout(_run_cli(env, "outcome", "approve", topic_id))
    approved = approved_payload["result"]
    assert isinstance(approved, dict)
    assert approved["state"] == "approved"

    # Every CLI command above starts a new product subprocess.  This final
    # process verifies only the public persisted outcome envelope, not an
    # in-memory Application or private Store.
    observed_payload = _json_stdout(_run_cli(env, "outcome", "get", topic_id))
    observed = observed_payload["result"]
    assert isinstance(observed, dict)
    assert observed["brief"]["effective_state"] == "approved"
    assert observed["evidence"][0]["locator"] == f"pkv://entries/{knowledge_id}"

    # The SQLite pointer is structural authority, but its immutable Markdown
    # projection must still be present and digest-matched.  Model a user-side
    # alteration, then use one more real CLI process to observe the safe state.
    relative_path = brief["markdown_relative_path"]
    assert isinstance(relative_path, str)
    projection_path = data_root / "outcomes" / relative_path
    projection_path.write_text("altered outcome projection", encoding="utf-8")
    projection_payload = _json_stdout(_run_cli(env, "outcome", "get", topic_id))
    projection = projection_payload["result"]
    assert isinstance(projection, dict)
    assert projection["brief"]["state"] == "approved"
    assert projection["brief"]["projection_state"] == "stale"
    assert projection["brief"]["effective_state"] == "needs_review"
    assert projection["requires_review"] is True

    # A real missing file is the sole recoverable projection absence.  This is
    # another fresh CLI process, so it cannot observe a prior Application.
    projection_path.unlink()
    missing_payload = _json_stdout(_run_cli(env, "outcome", "get", topic_id))
    missing = missing_payload["result"]
    assert isinstance(missing, dict)
    assert missing["brief"]["projection_state"] == "unavailable"
    assert missing["brief"]["effective_state"] == "needs_review"

    # A non-file at the same pointer is not a missing projection.  It must
    # fail at the real public boundary without revealing the owned DataRoot.
    projection_path.mkdir()
    unsafe_projection = _json_stderr(_run_cli(env, "outcome", "get", topic_id))
    assert unsafe_projection["issues"][0]["code"] == "data_root_unsafe"
    assert str(data_root) not in json.dumps(unsafe_projection, ensure_ascii=False)


def test_outcome_cli_surfaces_markdown_source_drift_after_fresh_process(
    tmp_path: Path,
) -> None:
    """A user-owned Markdown edit must invalidate public Entry Evidence."""

    data_root = tmp_path / "outcome-cli-drift"
    knowledge_id = _seed_source(data_root)
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
    env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"

    topic_payload = _json_stdout(
        _run_cli(
            env,
            "outcome",
            "create-topic",
            "--title",
            "CLI Markdown drift",
            "--question",
            "Does a source edit invalidate an approved outcome?",
        )
    )
    topic = topic_payload["result"]
    assert isinstance(topic, dict)
    topic_id = topic["topic_id"]
    assert isinstance(topic_id, str)
    evidence_payload = _json_stdout(
        _run_cli(
            env,
            "outcome",
            "attach-evidence",
            topic_id,
            f"pkv://entries/{knowledge_id}",
        )
    )
    evidence = evidence_payload["result"]
    assert isinstance(evidence, dict)
    evidence_id = evidence["evidence_id"]
    assert isinstance(evidence_id, str)
    claim_payload = _json_stdout(
        _run_cli(
            env,
            "outcome",
            "create-claim",
            topic_id,
            "The source supports the scoped CLI drift claim.",
            "--state",
            "supported",
        )
    )
    claim = claim_payload["result"]
    assert isinstance(claim, dict)
    claim_id = claim["claim_id"]
    assert isinstance(claim_id, str)
    _json_stdout(
        _run_cli(
            env,
            "outcome",
            "link-evidence",
            claim_id,
            evidence_id,
            "--polarity",
            "support",
        )
    )
    _json_stdout(_run_cli(env, "outcome", "render", topic_id))
    _json_stdout(_run_cli(env, "outcome", "approve", topic_id))

    source_path = _source_markdown_path(data_root, knowledge_id)
    original = source_path.read_text(encoding="utf-8")
    # Test-owned fixture mutation models a user/Git Markdown edit.  All Outcome
    # observations below still enter only through new product CLI processes.
    source_path.write_text(
        original.replace(
            "cli-source-body-must-not-leak",
            "cli-markdown-source-changed-after-capture",
        ),
        encoding="utf-8",
    )

    drifted_payload = _json_stdout(_run_cli(env, "outcome", "get", topic_id))
    drifted = drifted_payload["result"]
    assert isinstance(drifted, dict)
    assert drifted["evidence"][0]["recorded_state"] == "stale"
    assert drifted["claims"][0]["state"] == "insufficient"
    assert drifted["brief"]["effective_state"] == "needs_review"
    assert "cli-markdown-source-changed-after-capture" not in json.dumps(
        drifted_payload,
        ensure_ascii=False,
    )
    rejected = _json_stderr(_run_cli(env, "outcome", "render", topic_id))
    assert rejected["issues"][0]["code"] == "outcome_evidence_stale"
