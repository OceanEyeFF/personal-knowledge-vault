"""P1-B Topic → Evidence → Claim → research brief source-contract tests."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from src.application import KnowledgeApplication
from src.outcomes.models import EvidenceSuggestion
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import VaultWriteLease, write_lease_scope
from src.storage.markdown_store import Entry
from src.utils.config import Config

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _isolated_config(tmp_path: Path) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        environment={},
    )
    return Config(layout=layout)


def _seed_source_entry(application: KnowledgeApplication) -> int:
    """Prepare one real SQLite/Markdown source without using an outcome seam."""

    with write_lease_scope(application.config.layout):
        archived = application.storage_coordinator.archive(
            Entry(
                title="Outcome source title",
                source_type="text",
                content="source-body-must-not-appear-in-brief",
                tags=["outcome"],
                keywords=["traceability"],
            ),
            chunks=["source-body-must-not-appear-in-brief"],
        )
    assert archived.knowledge_id is not None
    return archived.knowledge_id


def test_research_brief_is_revisioned_bound_and_drift_invalidates_review(
    tmp_path: Path,
) -> None:
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)

    topic = application.create_outcome_topic(
        title="Evidence-bound inquiry",
        question="Does the selected source support the stated conclusion?",
    )
    suggestions = application.suggest_outcome_evidence(
        topic_id=topic.topic_id,
        query="Outcome source title",
    )
    assert suggestions.status == "success"
    assert suggestions.suggestions[0].knowledge_id == knowledge_id
    # P1-B suggestion is strict BM25: it may construct the BM25 retriever but
    # never enters the Provider-aware QueryRouter path.
    assert application._query_routers == {}
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The selected source supports this limited conclusion.",
        state="supported",
    )

    # A declared supported state must not be displayed as supported until the
    # Claim has an explicitly linked, current support Evidence item.
    pending = application.get_outcome_bundle(topic_id=topic.topic_id)
    assert pending.claims[0].state.value == "insufficient"

    with pytest.raises(PKVRuntimeError) as unlinked_approval:
        application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert unlinked_approval.value.code is ErrorCode.OUTCOME_APPROVAL_BLOCKED

    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    assert (
        application.get_outcome_bundle(topic_id=topic.topic_id).claims[0].state.value
        == "supported"
    )
    brief = application.render_outcome_research_brief(topic_id=topic.topic_id)

    assert brief.state.value == "awaiting_review"
    markdown_path = config.layout.outcomes_dir / brief.markdown_relative_path
    markdown = markdown_path.read_text(encoding="utf-8")
    assert f"pkv://entries/{knowledge_id}" in markdown
    assert "source-body-must-not-appear-in-brief" not in markdown
    assert str(config.layout.user_data_root) not in markdown

    approved = application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert approved.state.value == "approved"

    # Mutating the owned source DB is an isolated fixture action used to model a
    # later source revision.  The outcome reader must only overlay stale state;
    # it cannot write or silently rebind the captured digest.
    with (
        write_lease_scope(config.layout),
        application.sqlite_store.get_connection() as conn,
    ):
        conn.execute(
            "UPDATE knowledge_items SET content = ? WHERE knowledge_id = ?",
            ("source body changed after evidence capture", knowledge_id),
        )

    observed = application.get_outcome_bundle(topic_id=topic.topic_id)
    assert observed.evidence[0].recorded_state.value == "stale"
    assert observed.claims[0].state.value == "insufficient"
    assert observed.brief is not None
    assert observed.brief.state.value == "approved"
    assert observed.brief.visible_state.value == "needs_review"

    # W3 observes source drift without changing the captured input contract or
    # the durable pointer.  Only an explicit input mutation may change those.
    stored_before = application.outcome_service._stored_bundle(topic.topic_id)
    database_before = config.db_path.read_bytes()
    projection_before = markdown_path.read_bytes()
    with pytest.raises(PKVRuntimeError) as stale_render:
        application.render_outcome_research_brief(topic_id=topic.topic_id)
    assert stale_render.value.code is ErrorCode.OUTCOME_EVIDENCE_STALE

    persisted = application.get_outcome_research_brief(topic_id=topic.topic_id)
    assert persisted is not None
    assert persisted.state.value == "approved"
    assert persisted.visible_state.value == "needs_review"
    assert application.outcome_service._stored_bundle(topic.topic_id) == stored_before
    assert config.db_path.read_bytes() == database_before
    assert markdown_path.read_bytes() == projection_before
    from pkv_kernel import KnowledgeKernel, outcomes
    public_brief = outcomes.get_research_brief_v2(
        topic_id=topic.topic_id, kernel=KnowledgeKernel._from_application(application),
    )
    assert public_brief.review_confirmation is None
    assert application._query_routers == {}


def test_approval_requires_a_brief_and_explicit_current_evidence_link(
    tmp_path: Path,
) -> None:
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Approval gates",
        question="Can a brief be approved without explicit evidence links?",
    )

    with pytest.raises(PKVRuntimeError) as missing_brief:
        application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert missing_brief.value.code is ErrorCode.OUTCOME_APPROVAL_BLOCKED

    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="This remains a draft until a reviewer makes it explicit.",
    )
    application.render_outcome_research_brief(topic_id=topic.topic_id)

    with pytest.raises(PKVRuntimeError) as draft_blocked:
        application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert draft_blocked.value.code is ErrorCode.OUTCOME_APPROVAL_BLOCKED

    application.revise_outcome_claim(
        claim_id=claim.claim_id,
        state="supported",
        expected_revision=claim.revision,
    )
    with pytest.raises(PKVRuntimeError) as unlinked_blocked:
        application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert unlinked_blocked.value.code is ErrorCode.OUTCOME_APPROVAL_BLOCKED

    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    application.render_outcome_research_brief(topic_id=topic.topic_id)
    approved = application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert approved.state.value == "approved"

    returned = application.return_outcome_research_brief(topic_id=topic.topic_id)
    assert returned.state.value == "returned"
    with pytest.raises(PKVRuntimeError) as returned_approval:
        application.approve_outcome_research_brief(topic_id=topic.topic_id)
    assert returned_approval.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    rerendered = application.render_outcome_research_brief(topic_id=topic.topic_id)
    assert rerendered.revision == approved.revision + 1
    assert rerendered.state.value == "awaiting_review"


def test_counter_evidence_and_degraded_retrieval_remain_explicit_in_brief(
    tmp_path: Path,
) -> None:
    """A degraded counter-evidence trail never becomes an implicit fact claim."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Counter evidence",
        question="Does a declared counter-evidence limit the resulting brief?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
        retrieval_status="degraded",
        limitation_codes=("retrieval_degraded",),
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The available source leaves this claim contested.",
        state="contested",
        limitation_codes=("scope_limited",),
    )
    assert (
        application.get_outcome_bundle(topic_id=topic.topic_id).claims[0].state.value
        == "insufficient"
    )

    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="counter",
    )
    observed = application.get_outcome_bundle(topic_id=topic.topic_id)
    assert observed.claims[0].state.value == "contested"
    assert observed.evidence[0].retrieval_status == "degraded"
    assert observed.evidence[0].limitation_codes == ("retrieval_degraded",)

    brief = application.render_outcome_research_brief(topic_id=topic.topic_id)
    markdown = (config.layout.outcomes_dir / brief.markdown_relative_path).read_text(
        encoding="utf-8"
    )
    assert "`counter`" in markdown
    assert "Retrieval status: `degraded`" in markdown
    assert "`retrieval_degraded`" in markdown
    assert "`scope_limited`" in markdown
    assert (
        application.approve_outcome_research_brief(topic_id=topic.topic_id).state.value
        == "approved"
    )


def test_outcome_mutation_respects_an_existing_root_writer_lease(tmp_path: Path) -> None:
    """Outcome writes do not bypass the data-root single-writer contract."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)

    with VaultWriteLease(config.layout):
        with pytest.raises(PKVRuntimeError) as busy:
            application.create_outcome_topic(
                title="Busy outcome mutation",
                question="Can an unrelated writer lease be bypassed?",
            )
    assert busy.value.code is ErrorCode.WRITE_BUSY
    assert busy.value.recoverable is True

    # Releasing the independent holder admits a normal mutation; no queued or
    # half-created Topic is used to hide the prior contention.
    created = application.create_outcome_topic(
        title="Lease released",
        question="Can a new explicit mutation run once the writer is free?",
    )
    assert created.revision == 1


def test_markdown_primary_drift_and_delete_invalidate_entry_evidence_after_restart(
    tmp_path: Path,
) -> None:
    """Entry Evidence requires matching Markdown and SQLite source projections."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Markdown source drift",
        question="Does an externally changed Markdown source remain valid evidence?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The source supports the scoped Markdown-drift claim.",
        state="supported",
    )
    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    application.render_outcome_research_brief(topic_id=topic.topic_id)
    application.approve_outcome_research_brief(topic_id=topic.topic_id)

    row = application.sqlite_store.query_by_id(knowledge_id)
    assert isinstance(row, dict)
    file_path = row["file_path"]
    assert isinstance(file_path, str)
    markdown_path = config.layout.vault_dir / file_path
    original = markdown_path.read_text(encoding="utf-8")
    # This is a test-owned simulation of a user/Git Markdown edit.  SQLite and
    # chunks are intentionally untouched, so the new revalidation must not
    # silently retain the old captured source revision.
    markdown_path.write_text(
        original.replace(
            "source-body-must-not-appear-in-brief",
            "markdown-body-changed-after-evidence-capture",
        ),
        encoding="utf-8",
    )

    restarted = KnowledgeApplication(config)
    drifted = restarted.get_outcome_bundle(topic_id=topic.topic_id)
    assert drifted.evidence[0].recorded_state.value == "stale"
    assert drifted.claims[0].state.value == "insufficient"
    assert drifted.brief is not None
    assert drifted.brief.visible_state.value == "needs_review"

    with pytest.raises(PKVRuntimeError) as stale_render:
        restarted.render_outcome_research_brief(topic_id=topic.topic_id)
    assert stale_render.value.code is ErrorCode.OUTCOME_EVIDENCE_STALE

    markdown_path.unlink()
    deleted = KnowledgeApplication(config).get_outcome_bundle(topic_id=topic.topic_id)
    assert deleted.evidence[0].recorded_state.value == "unavailable"
    assert deleted.claims[0].state.value == "insufficient"
    assert deleted.brief is not None
    assert deleted.brief.visible_state.value == "needs_review"


def test_brief_pointer_revalidates_its_immutable_markdown_projection(
    tmp_path: Path,
) -> None:
    """A SQLite pointer never treats an altered or missing brief as approved."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Brief projection integrity",
        question="Does the stored brief pointer still name its immutable projection?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The selected source supports the projection-integrity claim.",
        state="supported",
    )
    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    brief = application.render_outcome_research_brief(topic_id=topic.topic_id)
    application.approve_outcome_research_brief(topic_id=topic.topic_id)
    projection_path = config.layout.outcomes_dir / brief.markdown_relative_path

    # An external edit cannot silently turn the SQLite pointer into a valid
    # approved brief.  A fresh Application models a fresh product process.
    projection_path.write_text("changed outcome projection", encoding="utf-8")
    altered = KnowledgeApplication(config).get_outcome_bundle(topic_id=topic.topic_id)
    assert altered.brief is not None
    assert altered.brief.state.value == "approved"
    assert altered.brief.projection_state == "stale"
    assert altered.brief.visible_state.value == "needs_review"
    assert altered.requires_review is True

    with pytest.raises(PKVRuntimeError) as approval_blocked:
        KnowledgeApplication(config).approve_outcome_research_brief(
            topic_id=topic.topic_id
        )
    assert approval_blocked.value.code is ErrorCode.OUTCOME_STATE_CONFLICT

    repaired = KnowledgeApplication(config).render_outcome_research_brief(
        topic_id=topic.topic_id
    )
    assert repaired.revision == 2
    assert repaired.projection_state == "current"

    repaired_path = config.layout.outcomes_dir / repaired.markdown_relative_path
    repaired_path.unlink()
    missing = KnowledgeApplication(config).get_outcome_bundle(topic_id=topic.topic_id)
    assert missing.brief is not None
    assert missing.brief.projection_state == "unavailable"
    assert missing.brief.visible_state.value == "needs_review"


@pytest.mark.parametrize(
    "failure_factory",
    (
        lambda: PermissionError(errno.EACCES, "permission denied"),
        lambda: OSError(errno.EIO, "I/O failure"),
    ),
    ids=("permission", "io"),
)
def test_brief_pointer_read_faults_fail_closed_instead_of_becoming_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_factory: object,
) -> None:
    """Only a provably absent projection has the recoverable unavailable state."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Projection read error",
        question="Do unknown filesystem failures remain fail-closed?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The selected source supports the filesystem-boundary claim.",
        state="supported",
    )
    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    application.render_outcome_research_brief(topic_id=topic.topic_id)

    def fail_open(_layout: RuntimeLayout, *_args: object, **_kwargs: object) -> object:
        del _layout
        factory = failure_factory
        assert callable(factory)
        raise factory()

    # This is a unit-level filesystem-gateway fault injection, not an
    # Application/lifecycle success fake.  The public adapters map the raised
    # stable code to their generic, path-free failure envelope.
    monkeypatch.setattr(RuntimeLayout, "open_user_file", fail_open)
    with pytest.raises(PKVRuntimeError) as rejected:
        KnowledgeApplication(config).get_outcome_bundle(topic_id=topic.topic_id)
    assert rejected.value.code is ErrorCode.PATH_STATE_UNDETERMINED
    assert rejected.value.recoverable is True
    assert str(config.layout.user_data_root) not in str(rejected.value)


def test_outcome_authored_text_rejects_private_references_before_persistence(
    tmp_path: Path,
) -> None:
    """Topic/Claim/query/suggestion text cannot become a public secret channel."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    private_path = r"C:\private-outcome-canary\brief.md"
    credential_url = "https://reader:token-do-not-echo@example.invalid/brief"
    embedded_path = "The source was at /private-outcome-canary/brief.md."
    query_with_token = "https://example.invalid/brief?token=token-do-not-echo"
    file_uri = "file:///C:/private-outcome-canary/brief.md"

    with pytest.raises(ValueError):
        application.create_outcome_topic(
            title=private_path,
            question="Does a rejected title create a topic?",
        )
    with pytest.raises(ValueError):
        application.create_outcome_topic(
            title="Safe title",
            question=credential_url,
        )

    topic = application.create_outcome_topic(
        title="Private reference rejection",
        question="Does the public contract reject unsafe authored text?",
    )
    with pytest.raises(ValueError):
        application.create_outcome_claim(
            topic_id=topic.topic_id,
            statement=embedded_path,
        )
    with pytest.raises(ValueError):
        application.suggest_outcome_evidence(
            topic_id=topic.topic_id,
            query=query_with_token,
        )
    with pytest.raises(ValueError):
        EvidenceSuggestion(
            knowledge_id=1,
            locator="pkv://entries/1",
            title=file_uri,
            score=0.5,
        )
    assert application.get_outcome_bundle(topic_id=topic.topic_id).claims == ()


def test_initial_entry_binding_rejects_a_markdown_sqlite_split_brain(
    tmp_path: Path,
) -> None:
    """A first Evidence attachment cannot bless an already divergent source."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Split-brain source",
        question="Can an already divergent source be attached as evidence?",
    )

    row = application.sqlite_store.query_by_id(knowledge_id)
    assert isinstance(row, dict)
    file_path = row["file_path"]
    assert isinstance(file_path, str)
    markdown_path = config.layout.vault_dir / file_path
    markdown_path.write_text(
        markdown_path.read_text(encoding="utf-8").replace(
            "source-body-must-not-appear-in-brief",
            "divergent-markdown-body-before-first-evidence-binding",
        ),
        encoding="utf-8",
    )

    with pytest.raises(PKVRuntimeError) as rejected:
        application.attach_outcome_evidence(
            topic_id=topic.topic_id,
            locator=f"pkv://entries/{knowledge_id}",
        )
    assert rejected.value.code is ErrorCode.OUTCOME_EVIDENCE_STALE
    assert application.get_outcome_bundle(topic_id=topic.topic_id).evidence == ()


def test_explicit_unlink_and_detach_never_rebind_evidence(tmp_path: Path) -> None:
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Explicit evidence removal",
        question="Can a reviewer remove one bound source without a replacement?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="This source may support the scoped claim.",
        state="supported",
    )
    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    assert (
        application.get_outcome_bundle(topic_id=topic.topic_id).claims[0].state.value
        == "supported"
    )

    application.unlink_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
    )
    unlinked = application.get_outcome_bundle(topic_id=topic.topic_id)
    assert unlinked.claims[0].evidence_links == ()
    assert unlinked.claims[0].state.value == "insufficient"

    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    application.detach_outcome_evidence(evidence.evidence_id)
    detached = application.get_outcome_bundle(topic_id=topic.topic_id)
    assert detached.evidence == ()
    assert detached.claims[0].evidence_links == ()
    assert detached.claims[0].state.value == "insufficient"


def test_render_recovers_an_exact_orphaned_projection_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after Markdown publish cannot permanently block its SQLite pointer."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Projection recovery",
        question="Can a deterministic orphan projection be recovered safely?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The bound source supports the limited recovery claim.",
        state="supported",
    )
    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )

    def crash_after_publish(**_kwargs: object) -> None:
        raise PKVRuntimeError(
            ErrorCode.OUTCOME_STATE_CONFLICT,
            "injected pointer publication interruption",
            stage="outcome_brief",
            recoverable=True,
        )

    monkeypatch.setattr(
        application.outcome_service._outcome_store,
        "upsert_rendered_brief",
        crash_after_publish,
    )
    with pytest.raises(PKVRuntimeError) as interrupted:
        application.render_outcome_research_brief(topic_id=topic.topic_id)
    assert interrupted.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert application.get_outcome_research_brief(topic_id=topic.topic_id) is None

    topic_dir = config.layout.outcomes_dir / f"topic-{topic.topic_id}"
    orphaned = tuple(topic_dir.glob("*.md"))
    assert len(orphaned) == 1
    orphan_bytes = orphaned[0].read_bytes()

    # A new Application models a fresh product process.  It can only reuse the
    # exact immutable bytes; a mismatched file remains a fail-closed conflict.
    restarted = KnowledgeApplication(config)
    recovered = restarted.render_outcome_research_brief(topic_id=topic.topic_id)
    assert recovered.revision == 1
    assert (
        recovered.markdown_relative_path
        == orphaned[0].relative_to(config.layout.outcomes_dir).as_posix()
    )
    assert (
        config.layout.outcomes_dir / recovered.markdown_relative_path
    ).read_bytes() == orphan_bytes


def test_changed_input_after_orphaned_first_projection_uses_new_immutable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed first render must not be blocked by an unmatched old orphan."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="Changed orphan input",
        question="Can a changed first brief avoid a stale orphan projection?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The initial source supports this recovery claim.",
        state="supported",
    )
    application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )

    def crash_after_publish(**_kwargs: object) -> None:
        raise PKVRuntimeError(
            ErrorCode.OUTCOME_STATE_CONFLICT,
            "injected pointer publication interruption",
            stage="outcome_brief",
            recoverable=True,
        )

    monkeypatch.setattr(
        application.outcome_service._outcome_store,
        "upsert_rendered_brief",
        crash_after_publish,
    )
    with pytest.raises(PKVRuntimeError):
        application.render_outcome_research_brief(topic_id=topic.topic_id)

    topic_dir = config.layout.outcomes_dir / f"topic-{topic.topic_id}"
    old_orphans = tuple(topic_dir.glob("*.md"))
    assert len(old_orphans) == 1
    old_path = old_orphans[0]
    old_bytes = old_path.read_bytes()

    application.revise_outcome_claim(
        claim_id=claim.claim_id,
        statement="The revised source supports this recovery claim.",
        expected_revision=claim.revision,
    )
    recovered = KnowledgeApplication(config).render_outcome_research_brief(
        topic_id=topic.topic_id
    )

    new_path = config.layout.outcomes_dir / recovered.markdown_relative_path
    assert recovered.revision == 1
    assert new_path != old_path
    assert old_path.read_bytes() == old_bytes
    assert new_path.is_file()
    assert len(tuple(topic_dir.glob("*.md"))) == 2
