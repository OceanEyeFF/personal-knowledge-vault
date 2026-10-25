"""OC-02/04/05/06: Outcome commits across real process boundaries.

The parent injects an exception at the real Store pointer-commit seam; this is
not an OS kill test.  A fresh child runs the same pytest node through the
existing offline entrypoint and uses only public Outcome v2 read/render calls.
All source data, databases, projections and process reports are synthetic.
OC-02 additionally exits an owned child immediately after a real input commit;
another fresh child must observe the complete state without retrying mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from pkv_kernel import bootstrap_kernel, outcomes
from src.application import KnowledgeApplication
from src.outcomes.models import ResearchBriefRecord
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from tests.offline_runtime import prepare_offline_child_env
from tests.unit.test_traceable_outcomes import _isolated_config, _seed_source_entry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_NODE_ENV = "PKV_W3_OUTCOME_WORKER_NODE"
_REQUEST_NAME = "w3-outcome-process-request.json"
_RESULT_NAME = "w3-outcome-process-result.json"
_CAPTURED_STATEMENT = "This is the exact input captured by the interrupted publication."
_CHANGED_STATEMENT = "The input changed after the interrupted immutable publication."
_POINTER_FIELDS = (
    "outcome_id",
    "revision",
    "markdown_relative_path",
    "content_sha256",
    "publication_id",
)


@dataclass(frozen=True)
class _PreparedPublication:
    application: KnowledgeApplication
    topic_id: str
    outcome_id: str
    claim_id: str
    claim_revision: int
    previous_brief: ResearchBriefRecord | None


def _forbid_providers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def factory(name: str):
        def construct(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError("Outcome publication must not construct a Provider")

        return construct

    # Tripwires do not replace product reads, rendering, Store writes or leases.
    monkeypatch.setattr(KnowledgeApplication, "_create_embedder", factory("embedding"))
    monkeypatch.setattr(KnowledgeApplication, "_create_deepseek_client", factory("llm"))
    return calls


def _prepare_publication(tmp_path: Path, candidate_revision: int) -> _PreparedPublication:
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="W3 immutable publication process recovery",
        question="Does a new process recover the exact publication binding?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The selected synthetic source supports the initial claim.",
        state="supported",
    )
    claim = application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    # First-render interruption must retain the identity reserved at Topic
    # creation, even though no brief pointer has ever existed.
    bound_topic = application.get_outcome_bundle(topic_id=topic.topic_id).topic
    assert bound_topic.outcome_id is not None
    assert topic.outcome_id == bound_topic.outcome_id
    previous: ResearchBriefRecord | None = None
    for revision in range(1, candidate_revision):
        previous = application.render_outcome_research_brief(topic_id=topic.topic_id)
        assert previous.revision == revision
        assert previous.outcome_id == bound_topic.outcome_id

    claim = application.revise_outcome_claim(
        claim_id=claim.claim_id,
        statement=_CAPTURED_STATEMENT,
        expected_revision=claim.revision,
    )
    return _PreparedPublication(
        application=application,
        topic_id=topic.topic_id,
        outcome_id=bound_topic.outcome_id,
        claim_id=claim.claim_id,
        claim_revision=claim.revision,
        previous_brief=previous,
    )


def _pointer(brief: ResearchBriefRecord | None) -> dict[str, Any] | None:
    if brief is None:
        return None
    return {field: getattr(brief, field) for field in _POINTER_FIELDS}


def _assert_public_brief(payload: dict[str, Any], expected: dict[str, Any]) -> None:
    assert payload["schema"] == "pkv.outcome.brief.v2"
    for field, value in expected.items():
        assert payload[field] == value, field
    assert payload["binding_state"] == "verified"
    assert payload["projection_state"] == "current"
    assert payload["state"] == "awaiting_review"
    assert payload["effective_state"] == "awaiting_review"
    assert payload["rendered_input_revision"] == payload["current_input_revision"]
    assert payload["rendered_input_digest"] == payload["current_input_digest"]
    assert payload["render_contract_version"] == payload["current_render_contract_version"]
    assert payload["review_confirmation"] == {
        "outcome_id": payload["outcome_id"],
        "expected_revision": payload["revision"],
        "content_sha256": payload["content_sha256"],
        "expected_input_revision": payload["current_input_revision"],
    }


def _run_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_revision: int,
    changed_input: bool | None,
) -> None:
    data_root = Path(os.environ["DATA_DIR"])
    request_path = data_root / "reports" / _REQUEST_NAME
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["parent_pid"] != os.getpid()
    assert payload["candidate_revision"] == candidate_revision
    assert payload["changed_input"] is changed_input
    provider_calls = _forbid_providers(monkeypatch)
    # Reconstruct precisely the parent's synthetic layout, never default Config.
    config = _isolated_config(data_root.parent)
    assert config.layout.user_data_root == data_root
    kernel = bootstrap_kernel(config)
    before_record = outcomes.get_research_brief_v2(
        topic_id=payload["topic_id"], kernel=kernel
    )
    before = None if before_record is None else before_record.to_dict()

    if payload["action"] == "render":
        expected_previous = payload["previous_pointer"]
        if expected_previous is None:
            assert before is None, "A read must not adopt the first orphan publication"
        else:
            assert before is not None
            assert {field: before[field] for field in _POINTER_FIELDS} == expected_previous
        rendered = outcomes.render_research_brief_v2(
            topic_id=payload["topic_id"], kernel=kernel
        )
        after = rendered.to_dict()
    else:
        assert payload["action"] == "get"
        assert before is not None
        after = before

    _assert_public_brief(after, payload["expected_binding"])
    by_topic = outcomes.get_research_brief_v2(topic_id=payload["topic_id"], kernel=kernel)
    by_outcome = outcomes.get_research_brief_v2(
        outcome_id=payload["outcome_id"], kernel=kernel
    )
    assert by_topic is not None and by_outcome is not None
    assert by_topic.to_dict() == after == by_outcome.to_dict()
    assert provider_calls == []
    (data_root / "reports" / _RESULT_NAME).write_text(
        json.dumps(
            {
                "child_pid": os.getpid(),
                "parent_pid": payload["parent_pid"],
                "before": before,
                "brief": after,
                "provider_calls": provider_calls,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _child_result(
    prepared: _PreparedPublication,
    request: pytest.FixtureRequest,
    payload: dict[str, Any],
    *,
    expected_exit: int = 0,
) -> dict[str, Any]:
    config = prepared.application.config
    data_root = config.layout.user_data_root
    reports = data_root / "reports"
    reports.mkdir(exist_ok=True)
    (reports / _REQUEST_NAME).write_text(
        json.dumps({"parent_pid": os.getpid(), **payload}, sort_keys=True),
        encoding="utf-8",
    )
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": data_root,
            "DB_PATH": config.db_path,
            "VAULT_DIR": config.vault_dir,
            "VECTOR_DIR": config.vector_index_dir,
            "LOG_DIR": config.log_dir,
            "TMP_DIR": config.tmp_dir,
        },
    )
    env[_WORKER_NODE_ENV] = request.node.nodeid
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"),
            "pytest",
            request.node.nodeid,
            "-q",
            "--tb=short",
            f"--basetemp={config.tmp_dir / 'pytest'}",
            "-o",
            f"cache_dir={config.tmp_dir / 'pytest-cache'}",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    assert completed.returncode == expected_exit, completed.stdout + completed.stderr
    if expected_exit:
        return {"child_exit_code": completed.returncode}
    observed = json.loads((reports / _RESULT_NAME).read_text(encoding="utf-8"))
    assert observed["child_pid"] != os.getpid()
    assert observed["parent_pid"] == os.getpid()
    assert observed["provider_calls"] == []
    return observed


def _expected_binding(
    prepared: _PreparedPublication, candidate_revision: int
) -> dict[str, Any]:
    topic = prepared.application.get_outcome_bundle(topic_id=prepared.topic_id).topic
    assert topic.outcome_id == prepared.outcome_id
    return {
        "topic_id": prepared.topic_id,
        "outcome_id": prepared.outcome_id,
        "revision": candidate_revision,
        "rendered_input_revision": topic.input_revision,
        "rendered_input_digest": topic.input_digest,
        "render_contract_version": topic.render_contract_version,
    }


@pytest.mark.parametrize("candidate_revision", (1, 2, 4), ids=("r1", "r2", "r4"))
@pytest.mark.parametrize("changed_input", (False, True), ids=("same_input", "changed_input"))
def test_orphan_publication_recovers_in_new_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    candidate_revision: int,
    changed_input: bool,
) -> None:
    worker_node = os.environ.get(_WORKER_NODE_ENV)
    if worker_node is not None:
        assert worker_node == request.node.nodeid
        _run_worker(
            monkeypatch,
            candidate_revision=candidate_revision,
            changed_input=changed_input,
        )
        return

    provider_calls = _forbid_providers(monkeypatch)
    prepared = _prepare_publication(tmp_path, candidate_revision)
    application = prepared.application
    outcomes_dir = application.config.layout.outcomes_dir
    original_files = {path: path.read_bytes() for path in outcomes_dir.rglob("*.md")}
    captured: list[dict[str, Any]] = []

    def fail_before_pointer_commit(**kwargs: Any) -> None:
        assert kwargs["revision"] == candidate_revision
        captured.append(kwargs)
        raise PKVRuntimeError(
            ErrorCode.OUTCOME_STATE_CONFLICT,
            "W3 injected interruption before pointer commit",
            stage="outcome_brief",
            recoverable=True,
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            application.outcome_service._outcome_store,
            "upsert_rendered_brief",
            fail_before_pointer_commit,
        )
        with pytest.raises(PKVRuntimeError, match="W3 injected interruption"):
            application.render_outcome_research_brief(topic_id=prepared.topic_id)
    assert len(captured) == 1
    interrupted = captured[0]
    assert interrupted["outcome_id"] == prepared.outcome_id
    previous = application.get_outcome_research_brief(topic_id=prepared.topic_id)
    assert _pointer(previous) == _pointer(prepared.previous_brief)
    orphan_paths = set(outcomes_dir.rglob("*.md")) - original_files.keys()
    assert len(orphan_paths) == 1
    orphan_path = orphan_paths.pop()
    assert orphan_path == outcomes_dir / interrupted["markdown_relative_path"]
    orphan_bytes = orphan_path.read_bytes()
    assert hashlib.sha256(orphan_bytes).hexdigest() == interrupted["content_sha256"]

    if changed_input:
        application.revise_outcome_claim(
            claim_id=prepared.claim_id,
            statement=_CHANGED_STATEMENT,
            expected_revision=prepared.claim_revision,
        )
    expected = _expected_binding(prepared, candidate_revision)
    if changed_input:
        assert (
            expected["rendered_input_revision"]
            == interrupted["expected_input_revision"] + 1
        )
        assert expected["rendered_input_digest"] != interrupted["expected_input_digest"]
    else:
        assert (
            expected["rendered_input_revision"]
            == interrupted["expected_input_revision"]
        )
        assert expected["rendered_input_digest"] == interrupted["expected_input_digest"]

    observed = _child_result(
        prepared,
        request,
        {
            "action": "render",
            "candidate_revision": candidate_revision,
            "changed_input": changed_input,
            "topic_id": prepared.topic_id,
            "outcome_id": prepared.outcome_id,
            "previous_pointer": _pointer(prepared.previous_brief),
            "expected_binding": expected,
        },
    )
    recovered = observed["brief"]
    recovered_path = outcomes_dir / recovered["markdown_relative_path"]
    assert (recovered_path != orphan_path) is changed_input
    assert (recovered["publication_id"] != interrupted["publication_id"]) is changed_input
    assert (
        hashlib.sha256(recovered_path.read_bytes()).hexdigest()
        == recovered["content_sha256"]
    )
    assert (
        recovered["content_sha256"] != interrupted["content_sha256"]
    ) is changed_input
    recovered_markdown = recovered_path.read_text(encoding="utf-8")
    expected_statement = _CHANGED_STATEMENT if changed_input else _CAPTURED_STATEMENT
    assert expected_statement in recovered_markdown
    if changed_input:
        assert _CAPTURED_STATEMENT not in recovered_markdown
    assert orphan_path.read_bytes() == orphan_bytes
    for path, content in original_files.items():
        assert path.read_bytes() == content
    expected_paths = set(original_files) | {orphan_path, recovered_path}
    assert set(outcomes_dir.rglob("*.md")) == expected_paths
    assert provider_calls == []


def _run_input_commit_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    data_root = Path(os.environ["DATA_DIR"])
    payload = json.loads((data_root / "reports" / _REQUEST_NAME).read_text(encoding="utf-8"))
    assert payload["parent_pid"] != os.getpid()
    provider_calls = _forbid_providers(monkeypatch)
    config = _isolated_config(data_root.parent)
    assert config.layout.user_data_root == data_root
    kernel = bootstrap_kernel(config)
    if payload["action"] == "mutate_then_exit":
        from src.storage.outcome_store import OutcomeStore
        update_claim = OutcomeStore.update_claim

        def committed_without_response(store, **kwargs):
            committed = update_claim(store, **kwargs)
            assert committed.statement == payload["statement"]
            assert committed.revision == payload["claim_revision"] + 1
            # The real Store context has committed before its method returns.
            # Exit this owned OS child before Service/Application can return or
            # unwind their audit/lease scopes; the parent never retries mutation.
            os._exit(73)

        monkeypatch.setattr(OutcomeStore, "update_claim", committed_without_response)
        outcomes.revise_claim(
            claim_id=payload["claim_id"], statement=payload["statement"],
            expected_revision=payload["claim_revision"], kernel=kernel,
        )
        raise AssertionError("The post-commit child exit was not reached")

    assert payload["action"] == "read_input_commit"

    def forbid_writer(_application):
        raise AssertionError("The fresh Outcome reader cannot acquire a writer lease")

    monkeypatch.setattr(KnowledgeApplication, "_write_lease_scope", forbid_writer)
    bundle = outcomes.get_outcome_v2(payload["topic_id"], kernel=kernel).to_dict()
    assert bundle["topic"]["input_revision"] == payload["input_revision"]
    assert bundle["topic"]["input_digest"] == payload["input_digest"]
    claim, = bundle["claims"]
    assert claim["claim_id"] == payload["claim_id"]
    assert claim["statement"] == payload["statement"]
    assert claim["revision"] == payload["claim_revision"] + 1
    brief = bundle["brief"]
    assert {field: brief[field] for field in _POINTER_FIELDS} == payload["pointer"]
    assert brief["state"] == brief["effective_state"] == "needs_review"
    assert brief["current_input_revision"] == payload["input_revision"]
    assert brief["current_input_digest"] == payload["input_digest"]
    assert brief["rendered_input_revision"] == payload["input_revision"] - 1
    assert brief["rendered_input_digest"] != payload["input_digest"]
    assert brief["projection_state"] == "current"
    assert brief["review_confirmation"] is None
    assert provider_calls == []
    (data_root / "reports" / _RESULT_NAME).write_text(json.dumps({
        "child_pid": os.getpid(), "parent_pid": payload["parent_pid"],
        "bundle": bundle, "provider_calls": provider_calls,
    }, sort_keys=True), encoding="utf-8")


def test_input_commit_survives_child_exit_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """OC-02: committed input/digest/invalidation survive an abrupt child exit."""
    if os.environ.get(_WORKER_NODE_ENV) is not None:
        assert os.environ[_WORKER_NODE_ENV] == request.node.nodeid
        _run_input_commit_worker(monkeypatch)
        return

    from src.outcomes.identity import outcome_input_digest
    provider_calls = _forbid_providers(monkeypatch)
    prepared = _prepare_publication(tmp_path, 1)
    application = prepared.application
    brief = application.render_outcome_research_brief_v2(topic_id=prepared.topic_id)
    application.approve_outcome_research_brief_v2(
        outcome_id=brief.outcome_id, expected_revision=brief.revision,
        content_sha256=brief.content_sha256,
        expected_input_revision=brief.rendered_input_revision,
    )
    before = application.outcome_service._stored_bundle(prepared.topic_id)
    assert before.brief.state.value == "approved"
    claim, = before.claims
    new_statement = "This input committed before the child could return its response."
    expected_revision = before.topic.input_revision + 1
    expected_digest = outcome_input_digest(
        topic=replace(before.topic, input_revision=expected_revision),
        evidence=before.evidence,
        claims=(replace(claim, statement=new_statement, revision=claim.revision + 1),),
    )
    payload = {
        "topic_id": prepared.topic_id, "claim_id": claim.claim_id,
        "claim_revision": claim.revision, "statement": new_statement,
        "input_revision": expected_revision, "input_digest": expected_digest,
        "pointer": _pointer(before.brief),
    }
    files = {path: path.read_bytes() for path in application.config.layout.outcomes_dir.rglob("*.md")}
    exited = _child_result(prepared, request, {**payload, "action": "mutate_then_exit"}, expected_exit=73)
    assert exited == {"child_exit_code": 73}
    assert not (application.config.layout.user_data_root / "reports" / _RESULT_NAME).exists()
    database_after_commit = application.config.db_path.read_bytes()
    observed = _child_result(prepared, request, {**payload, "action": "read_input_commit"})
    assert observed["bundle"]["topic"]["input_revision"] == expected_revision
    assert application.config.db_path.read_bytes() == database_after_commit
    assert {path: path.read_bytes() for path in application.config.layout.outcomes_dir.rglob("*.md")} == files
    assert provider_calls == []


@pytest.mark.parametrize("candidate_revision", (1, 2, 4), ids=("r1", "r2", "r4"))
def test_committed_pointer_survives_lost_response_in_new_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    candidate_revision: int,
) -> None:
    worker_node = os.environ.get(_WORKER_NODE_ENV)
    if worker_node is not None:
        assert worker_node == request.node.nodeid
        _run_worker(monkeypatch, candidate_revision=candidate_revision, changed_input=None)
        return

    provider_calls = _forbid_providers(monkeypatch)
    prepared = _prepare_publication(tmp_path, candidate_revision)
    application = prepared.application
    expected = _expected_binding(prepared, candidate_revision)
    store = application.outcome_service._outcome_store
    original_commit = store.upsert_rendered_brief
    committed: list[ResearchBriefRecord] = []

    def commit_then_lose_response(**kwargs: Any) -> None:
        assert kwargs["revision"] == candidate_revision
        committed.append(original_commit(**kwargs))
        raise PKVRuntimeError(
            ErrorCode.OUTCOME_STATE_CONFLICT,
            "W3 injected response loss after pointer commit",
            stage="outcome_brief",
            recoverable=True,
        )

    with monkeypatch.context() as patch:
        patch.setattr(store, "upsert_rendered_brief", commit_then_lose_response)
        with pytest.raises(PKVRuntimeError, match="W3 injected response loss"):
            application.render_outcome_research_brief(topic_id=prepared.topic_id)
    assert len(committed) == 1
    assert committed[0].revision == candidate_revision
    committed_pointer = _pointer(committed[0])
    assert committed_pointer is not None
    expected.update(committed_pointer)
    outcomes_dir = application.config.layout.outcomes_dir
    files_after_commit = {path: path.read_bytes() for path in outcomes_dir.rglob("*.md")}

    observed = _child_result(
        prepared,
        request,
        {
            "action": "get",
            "candidate_revision": candidate_revision,
            "changed_input": None,
            "topic_id": prepared.topic_id,
            "outcome_id": prepared.outcome_id,
            "expected_binding": expected,
        },
    )
    assert observed["before"] == observed["brief"]
    assert observed["brief"]["revision"] == candidate_revision
    assert set(outcomes_dir.rglob("*.md")) == set(files_after_commit)
    for path, content in files_after_commit.items():
        assert path.read_bytes() == content
    assert provider_calls == []
