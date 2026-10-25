"""W3 service publication, exact review and read-only binding contracts."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from src.application import KnowledgeApplication
from src.outcomes.models import EvidenceState, OutcomeState
from src.runtime.audit import AuditTrace, AuditTraceError
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from tests.unit.test_traceable_outcomes import _isolated_config, _seed_source_entry


@dataclass
class Scenario:
    app: KnowledgeApplication
    topic_id: str
    claim_id: str
    evidence_id: str
    source_id: int
    brief: object
    provider_calls: list[str]


@pytest.fixture
def scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scenario:
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    app = KnowledgeApplication(config)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("provider")
        raise AssertionError("Outcome work cannot construct a Provider")

    monkeypatch.setattr(KnowledgeApplication, "_create_embedder", forbidden)
    monkeypatch.setattr(KnowledgeApplication, "_create_deepseek_client", forbidden)
    source = _seed_source_entry(app)
    topic = app.create_outcome_topic(title="W3 inquiry", question="What does the bound source support?")
    evidence = app.attach_outcome_evidence(topic_id=topic.topic_id, locator=f"pkv://entries/{source}")
    claim = app.create_outcome_claim(topic_id=topic.topic_id, statement="The source supports this claim.", state="supported")
    app.link_outcome_claim_evidence(claim_id=claim.claim_id, evidence_id=evidence.evidence_id, polarity="support")
    brief = app.render_outcome_research_brief_v2(topic_id=topic.topic_id)
    result = Scenario(app, topic.topic_id, claim.claim_id, evidence.evidence_id, source, brief, calls)
    yield result
    assert calls == []


def confirmation(brief) -> dict:
    return dict(outcome_id=brief.outcome_id, expected_revision=brief.revision,
                content_sha256=brief.content_sha256, expected_input_revision=brief.rendered_input_revision)


def revise(scenario: Scenario, statement: str = "A revised supported statement."):
    current = scenario.app.get_outcome_bundle(topic_id=scenario.topic_id).claims[0]
    return scenario.app.revise_outcome_claim(claim_id=scenario.claim_id, statement=statement,
                                           expected_revision=current.revision)


def receipts(scenario: Scenario):
    with sqlite3.connect(scenario.app.config.db_path) as connection:
        return connection.execute("SELECT * FROM outcome_review_receipts ORDER BY receipt_id").fetchall()


@pytest.mark.parametrize("changed_input", [False, True], ids=["same_input_winner", "changed_input_winner"])
def test_pointer_cas_preserves_competing_publication(scenario, monkeypatch, changed_input):
    """A competitor after file publication cannot be rolled back by a stale CAS."""
    service = scenario.app.outcome_service
    store = service._outcome_store
    commit = store.upsert_rendered_brief
    winner = []

    def competing_commit(**kwargs):
        if changed_input:
            revise(scenario)
        other = KnowledgeApplication(scenario.app.config)
        winner.append(other.render_outcome_research_brief_v2(topic_id=scenario.topic_id))
        return commit(**kwargs)

    monkeypatch.setattr(store, "upsert_rendered_brief", competing_commit)
    with pytest.raises(PKVRuntimeError) as rejected:
        scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    current = KnowledgeApplication(scenario.app.config).get_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert current.revision == 2 and current.publication_id == winner[0].publication_id
    assert current.content_sha256 == winner[0].content_sha256
    files = tuple(scenario.app.config.layout.outcomes_dir.rglob("*.md"))
    assert len(files) == (3 if changed_input else 2)
    assert hashlib.sha256((scenario.app.config.layout.outcomes_dir / current.markdown_relative_path).read_bytes()).hexdigest() == current.content_sha256


def test_input_change_between_publication_and_pointer_cas_leaves_old_pointer(scenario, monkeypatch):
    store = scenario.app.outcome_service._outcome_store
    commit = store.upsert_rendered_brief

    def changed(**kwargs):
        revise(scenario)
        return commit(**kwargs)

    monkeypatch.setattr(store, "upsert_rendered_brief", changed)
    with pytest.raises(PKVRuntimeError) as rejected:
        scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    current = scenario.app.get_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert current.revision == scenario.brief.revision
    assert current.markdown_relative_path == scenario.brief.markdown_relative_path
    assert current.visible_state is OutcomeState.NEEDS_REVIEW
    assert len(tuple(scenario.app.config.layout.outcomes_dir.rglob("*.md"))) == 2


def test_same_input_orphan_with_different_bytes_is_never_overwritten(scenario, monkeypatch):
    store = scenario.app.outcome_service._outcome_store
    before = set(scenario.app.config.layout.outcomes_dir.rglob("*.md"))
    with monkeypatch.context() as fault:
        def fail(**kwargs):
            raise PKVRuntimeError(ErrorCode.OUTCOME_STATE_CONFLICT, "synthetic interruption")
        fault.setattr(store, "upsert_rendered_brief", fail)
        with pytest.raises(PKVRuntimeError):
            scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    orphan, = set(scenario.app.config.layout.outcomes_dir.rglob("*.md")) - before
    orphan.write_bytes(b"synthetic mismatched orphan\n")
    with pytest.raises(PKVRuntimeError) as rejected:
        KnowledgeApplication(scenario.app.config).render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert orphan.read_bytes() == b"synthetic mismatched orphan\n"
    assert scenario.app.get_outcome_research_brief_v2(topic_id=scenario.topic_id).revision == 1


@pytest.mark.parametrize("rerender", [False, True], ids=["input_changed", "new_pointer"])
@pytest.mark.parametrize("action", ["approve", "return"])
def test_exact_review_rejects_changed_input_or_pointer(scenario, rerender, action):
    reviewed = confirmation(scenario.brief)
    revise(scenario)
    if rerender:
        scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    method = getattr(scenario.app, f"{action}_outcome_research_brief_v2")
    with pytest.raises(PKVRuntimeError) as rejected:
        method(**reviewed, **({"reason_code": "other"} if action == "return" else {}))
    assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert receipts(scenario) == []
    current = scenario.app.get_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert current.state is (OutcomeState.AWAITING_REVIEW if rerender else OutcomeState.NEEDS_REVIEW)


@pytest.mark.parametrize("rerender", [False, True], ids=["input_changed", "new_pointer"])
@pytest.mark.parametrize("action", ["approve", "return"])
def test_review_commit_rechecks_input_and_pointer_after_service_precheck(scenario, monkeypatch, rerender, action):
    store = scenario.app.outcome_service._outcome_store
    commit = store.review_brief
    competitor = []

    def competing_review(**kwargs):
        revise(scenario)
        if rerender:
            competitor.append(scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id))
        return commit(**kwargs)

    monkeypatch.setattr(store, "review_brief", competing_review)
    method = getattr(scenario.app, f"{action}_outcome_research_brief_v2")
    with pytest.raises(PKVRuntimeError) as rejected:
        method(**confirmation(scenario.brief), **({"reason_code": "other"} if action == "return" else {}))
    assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert receipts(scenario) == []
    current = scenario.app.get_outcome_research_brief_v2(topic_id=scenario.topic_id)
    expected = competitor[0] if rerender else scenario.brief
    assert current.revision == expected.revision
    assert current.publication_id == expected.publication_id
    assert current.state is (OutcomeState.AWAITING_REVIEW if rerender else OutcomeState.NEEDS_REVIEW)


def test_review_receipts_replay_only_while_exact_target_state_remains(scenario):
    values = confirmation(scenario.brief)
    approved = scenario.app.approve_outcome_research_brief_v2(**values)
    replay = scenario.app.approve_outcome_research_brief_v2(**values)
    assert approved.idempotent is False and replay.idempotent is True
    assert replay.receipt_id == approved.receipt_id
    returned = scenario.app.return_outcome_research_brief_v2(**values, reason_code="needs_revision")
    again = scenario.app.return_outcome_research_brief_v2(**values, reason_code="needs_revision")
    assert returned.idempotent is False and again.idempotent is True
    assert again.receipt_id == returned.receipt_id != approved.receipt_id
    for callback in (
        lambda: scenario.app.approve_outcome_research_brief_v2(**values),
        lambda: scenario.app.return_outcome_research_brief_v2(**values, reason_code="other"),
    ):
        with pytest.raises(PKVRuntimeError) as rejected:
            callback()
        assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert len(receipts(scenario)) == 2
    assert scenario.app.get_outcome_research_brief_v2(topic_id=scenario.topic_id).state is OutcomeState.RETURNED


def test_receipt_insert_failure_rolls_back_review_state(scenario, monkeypatch):
    store = scenario.app.outcome_service._outcome_store
    original = store.get_connection
    denied = []

    @contextmanager
    def connection():
        with original() as db:
            def authorize(action, table, column, database, trigger):
                if action == sqlite3.SQLITE_INSERT and table == "outcome_review_receipts":
                    denied.append(table)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            db.set_authorizer(authorize)
            try:
                yield db
            finally:
                db.set_authorizer(None)

    monkeypatch.setattr(store, "get_connection", connection)
    with pytest.raises((sqlite3.DatabaseError, PKVRuntimeError)):
        scenario.app.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    assert denied == ["outcome_review_receipts"]
    assert receipts(scenario) == []
    assert KnowledgeApplication(scenario.app.config).get_outcome_research_brief_v2(topic_id=scenario.topic_id).state is OutcomeState.AWAITING_REVIEW


def test_committed_receipt_survives_response_or_audit_failure(scenario, monkeypatch):
    store = scenario.app.outcome_service._outcome_store
    commit = store.review_brief
    with monkeypatch.context() as fault:
        def lost_response(**kwargs):
            commit(**kwargs)
            raise RuntimeError("synthetic response loss after commit")
        fault.setattr(store, "review_brief", lost_response)
        with pytest.raises(RuntimeError):
            scenario.app.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    restarted = KnowledgeApplication(scenario.app.config)
    with monkeypatch.context() as fault:
        def audit_failure(*args):
            raise AuditTraceError()
        fault.setattr(restarted, "_finish_mutation_audit", audit_failure)
        replay = restarted.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    assert replay.idempotent and replay.brief.state is OutcomeState.APPROVED
    assert len(receipts(scenario)) == 1


def test_single_terminal_audit_failure_does_not_record_committed_review_as_failed(scenario, monkeypatch):
    append = AuditTrace.append
    phases = []

    def append_once_failed(trace, event):
        if event.get("operation") == "outcome_approve_v2":
            phases.append(event["phase"])
            if event["phase"] == "completed" and phases.count("completed") == 1:
                raise AuditTraceError()
        return append(trace, event)

    with monkeypatch.context() as fault:
        fault.setattr(AuditTrace, "append", append_once_failed)
        committed = scenario.app.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    assert phases == ["started", "completed"]
    assert committed.idempotent is False
    assert committed.brief.state is OutcomeState.APPROVED
    assert len(receipts(scenario)) == 1
    replay = KnowledgeApplication(scenario.app.config).approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    assert replay.idempotent is True and replay.receipt_id == committed.receipt_id
    assert len(receipts(scenario)) == 1


def test_failed_audit_append_cannot_replace_review_conflict(scenario, monkeypatch):
    revise(scenario)
    append = AuditTrace.append
    attempts = []

    def unavailable_failed_event(trace, event):
        if event.get("operation") == "outcome_approve_v2" and event["phase"] == "failed":
            attempts.append(event["failure"]["code"])
            raise AuditTraceError()
        return append(trace, event)

    monkeypatch.setattr(AuditTrace, "append", unavailable_failed_event)
    with pytest.raises(PKVRuntimeError) as rejected:
        scenario.app.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    assert rejected.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert attempts[0] == ErrorCode.OUTCOME_STATE_CONFLICT.value
    assert receipts(scenario) == []
    assert scenario.app.get_outcome_research_brief_v2(topic_id=scenario.topic_id).state is OutcomeState.NEEDS_REVIEW


@pytest.mark.parametrize("changed", ["source", "projection"])
def test_publish_rechecks_external_bytes_before_pointer_commit(scenario, monkeypatch, changed):
    service = scenario.app.outcome_service
    publish = service._publish_brief_markdown
    before = service._stored_bundle(scenario.topic_id)

    def changed_after_publication(**kwargs):
        path, digest = publish(**kwargs)
        if changed == "source":
            with sqlite3.connect(scenario.app.config.db_path) as db:
                db.execute("UPDATE knowledge_items SET content = ? WHERE knowledge_id = ?", ("source changed after publication", scenario.source_id))
        else:
            (scenario.app.config.layout.outcomes_dir / path).write_bytes(b"projection changed after publication\n")
        return path, digest

    monkeypatch.setattr(service, "_publish_brief_markdown", changed_after_publication)
    with pytest.raises(PKVRuntimeError) as rejected:
        scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rejected.value.code is (ErrorCode.OUTCOME_EVIDENCE_STALE if changed == "source" else ErrorCode.OUTCOME_STATE_CONFLICT)
    assert service._stored_bundle(scenario.topic_id) == before
    assert len(tuple(scenario.app.config.layout.outcomes_dir.rglob("*.md"))) == 2


@pytest.mark.parametrize("action", ["approve", "return"])
@pytest.mark.parametrize("changed", ["source_stale", "source_missing", "projection_stale", "projection_missing"])
def test_exact_review_rejects_external_drift_without_state_or_receipt_write(scenario, action, changed):
    if changed == "source_stale":
        with sqlite3.connect(scenario.app.config.db_path) as db:
            db.execute("UPDATE knowledge_items SET content = ? WHERE knowledge_id = ?", ("source changed before review", scenario.source_id))
    elif changed == "source_missing":
        row = scenario.app.sqlite_store.query_by_id(scenario.source_id)
        (scenario.app.config.vault_dir / row["file_path"]).unlink()
    else:
        projection = scenario.app.config.layout.outcomes_dir / scenario.brief.markdown_relative_path
        if changed == "projection_stale":
            projection.write_bytes(b"projection changed before review\n")
        else:
            projection.unlink()
    before = scenario.app.outcome_service._stored_bundle(scenario.topic_id)
    method = getattr(scenario.app, f"{action}_outcome_research_brief_v2")
    with pytest.raises(PKVRuntimeError) as rejected:
        method(**confirmation(scenario.brief), **({"reason_code": "other"} if action == "return" else {}))
    assert rejected.value.code is (ErrorCode.OUTCOME_EVIDENCE_STALE if changed.startswith("source") else ErrorCode.OUTCOME_APPROVAL_BLOCKED)
    assert scenario.app.outcome_service._stored_bundle(scenario.topic_id) == before
    assert receipts(scenario) == []


def test_read_detects_input_binding_mismatch_without_writer_or_write(scenario, monkeypatch):
    scenario.app.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    with sqlite3.connect(scenario.app.config.db_path) as db:
        db.execute("UPDATE outcome_briefs SET rendered_input_revision = rendered_input_revision + 1")
    before = scenario.app.config.db_path.read_bytes()
    reopened = KnowledgeApplication(scenario.app.config)
    def forbidden():
        raise AssertionError("read cannot acquire writer lease")
    monkeypatch.setattr(reopened, "_write_lease_scope", forbidden)
    read = reopened.get_outcome_bundle_v2(topic_id=scenario.topic_id)
    assert read.brief.state is OutcomeState.APPROVED
    assert read.brief.visible_state is OutcomeState.NEEDS_REVIEW
    assert read.brief.projection_state == "current"
    assert scenario.app.config.db_path.read_bytes() == before


def test_unknown_renderer_is_rejected_without_input_or_publication_change(scenario, monkeypatch):
    service = scenario.app.outcome_service
    stored = service._stored_bundle(scenario.topic_id)
    from src.outcomes.identity import outcome_input_digest
    topic = replace(stored.topic, render_contract_version="research-brief.future")
    topic = replace(topic, input_digest=outcome_input_digest(topic=topic, evidence=stored.evidence, claims=stored.claims))
    monkeypatch.setattr(service, "_stored_bundle", lambda _: replace(stored, topic=topic))
    before = set(scenario.app.config.layout.outcomes_dir.rglob("*.md"))
    with pytest.raises(PKVRuntimeError) as rejected:
        scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rejected.value.code is ErrorCode.OUTCOME_RENDER_CONTRACT_UNSUPPORTED
    assert set(scenario.app.config.layout.outcomes_dir.rglob("*.md")) == before
    assert KnowledgeApplication(scenario.app.config).get_outcome_bundle(topic_id=scenario.topic_id).topic == stored.topic


@pytest.mark.parametrize("recorded_state", [EvidenceState.STALE, EvidenceState.UNAVAILABLE])
def test_recovered_source_requires_explicit_refresh_of_persisted_noncurrent_input(scenario, recorded_state):
    # A v1 observer could persist this state before the source recovered.  The
    # source now matches the captured digest, but the stored renderer input does
    # not: do not create insufficient Markdown that live review calls supported.
    service = scenario.app.outcome_service
    with scenario.app._write_lease_scope():
        service._outcome_store.update_evidence_recorded_states({scenario.evidence_id: recorded_state})
    stored = service._stored_bundle(scenario.topic_id)
    assert stored.evidence[0].recorded_state is recorded_state
    assert scenario.app.get_outcome_bundle_v2(topic_id=scenario.topic_id).evidence[0].recorded_state is EvidenceState.CURRENT
    before_files = set(scenario.app.config.layout.outcomes_dir.rglob("*.md"))
    with pytest.raises(PKVRuntimeError) as rejected:
        scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rejected.value.code is ErrorCode.OUTCOME_EVIDENCE_STALE
    assert service._stored_bundle(scenario.topic_id) == stored
    assert set(scenario.app.config.layout.outcomes_dir.rglob("*.md")) == before_files
    assert receipts(scenario) == []

    scenario.app.detach_outcome_evidence(evidence_id=scenario.evidence_id)
    refreshed = scenario.app.attach_outcome_evidence(topic_id=scenario.topic_id, locator=f"pkv://entries/{scenario.source_id}")
    scenario.app.link_outcome_claim_evidence(claim_id=scenario.claim_id, evidence_id=refreshed.evidence_id, polarity="support")
    rendered = scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    assert rendered.revision == 2 and rendered.rendered_input_revision > stored.topic.input_revision
    approved = scenario.app.approve_outcome_research_brief_v2(**confirmation(rendered))
    assert approved.brief.state is OutcomeState.APPROVED


def test_render_bytes_do_not_depend_on_effective_claim_overlay(scenario, monkeypatch):
    """An observed overlay must not change bytes under an unchanged input hash."""
    from src.outcomes.models import ClaimState
    service = scenario.app.outcome_service
    observed = service._bundle_from_store(scenario.topic_id)
    revalidate = service._revalidated_bundle
    def changed_observation(bundle):
        current = revalidate(bundle)
        return replace(current, claims=tuple(replace(claim, state=ClaimState.INSUFFICIENT) for claim in current.claims))
    monkeypatch.setattr(service, "_revalidated_bundle", changed_observation)
    rendered = scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
    text = (scenario.app.config.layout.outcomes_dir / rendered.markdown_relative_path).read_text(encoding="utf-8")
    assert "### C1: `supported`" in text
    assert "### C1: `insufficient`" not in text
    assert rendered.rendered_input_digest == observed.topic.input_digest


@pytest.mark.parametrize("action", ["read", "render", "review"])
def test_source_io_uncertainty_fails_closed_without_mutating_input(scenario, monkeypatch, action):
    before = scenario.app.outcome_service._stored_bundle(scenario.topic_id)
    def failure(*args, **kwargs):
        raise PermissionError("synthetic source read denied")
    monkeypatch.setattr(scenario.app.markdown_store, "load", failure)
    with pytest.raises(PKVRuntimeError) as rejected:
        if action == "read":
            scenario.app.get_outcome_bundle_v2(topic_id=scenario.topic_id)
        elif action == "render":
            scenario.app.render_outcome_research_brief_v2(topic_id=scenario.topic_id)
        else:
            scenario.app.approve_outcome_research_brief_v2(**confirmation(scenario.brief))
    assert rejected.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert scenario.app.outcome_service._stored_bundle(scenario.topic_id) == before
    assert receipts(scenario) == []
