"""W1 executable regressions for R-03/R-04/R-05 (intentionally red today).

These are in-process, synthetic fault tests using existing product seams.  A
fresh Application verifies durable state, but is not evidence of a real process
restart or of the future review-v2 API.  The unversioned review cases remain a
known gap through the major-1 compatibility stage and close only at W5.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from pkv_kernel import KnowledgeKernel, outcomes
from src.application import KnowledgeApplication
from src.outcomes.models import ClaimRecord, OutcomeState, ResearchBriefRecord, TopicRecord
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.write_lease import write_lease_scope
from tests.unit.test_traceable_outcomes import _isolated_config, _seed_source_entry


@dataclass(frozen=True)
class PreparedOutcome:
    application: KnowledgeApplication
    topic: TopicRecord
    claim: ClaimRecord
    brief: ResearchBriefRecord


@pytest.fixture
def prepared_outcome(tmp_path: Path) -> PreparedOutcome:
    """Use the established real SQLite/Markdown fixture and Provider-free path."""

    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    topic = application.create_outcome_topic(
        title="W1 outcome regression",
        question="Does the reviewed publication stay bound to its inputs?",
    )
    evidence = application.attach_outcome_evidence(
        topic_id=topic.topic_id,
        locator=f"pkv://entries/{knowledge_id}",
    )
    claim = application.create_outcome_claim(
        topic_id=topic.topic_id,
        statement="The original claim is the one presented for review.",
        state="supported",
    )
    claim = application.link_outcome_claim_evidence(
        claim_id=claim.claim_id,
        evidence_id=evidence.evidence_id,
        polarity="support",
    )
    brief = application.render_outcome_research_brief(topic_id=topic.topic_id)
    assert brief.revision == 1
    assert brief.visible_state is OutcomeState.AWAITING_REVIEW
    return PreparedOutcome(application, topic, claim, brief)


def test_r03_input_and_brief_invalidation_roll_back_together(
    prepared_outcome: PreparedOutcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OC-01: failing the brief-state UPDATE cannot leave a committed new Claim."""

    application = prepared_outcome.application
    topic_id = prepared_outcome.topic.topic_id
    store = application.outcome_service._outcome_store
    # Seed a test-owned historical state under the root lease.  This fixture
    # must not require the unsafe unversioned review API/Store setter to survive.
    with write_lease_scope(application.config.layout), store.get_connection() as connection:
        connection.execute(
            "UPDATE outcome_briefs SET state = ? WHERE topic_id = ?",
            (OutcomeState.APPROVED.value, topic_id),
        )
    before = application.get_outcome_bundle(topic_id=topic_id)
    assert before.brief is not None
    assert before.brief.visible_state is OutcomeState.APPROVED

    original_connection = store.get_connection
    denied_updates: list[tuple[str, str]] = []

    def deny_brief_state_update(
        action: int,
        table: str | None,
        column: str | None,
        _database: str | None,
        _trigger: str | None,
    ) -> int:
        if (
            action == sqlite3.SQLITE_UPDATE
            and table == "outcome_briefs"
            and column == "state"
        ):
            denied_updates.append((table, column))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def connection_with_state_fault() -> Iterator[sqlite3.Connection]:
        with original_connection() as connection:
            connection.set_authorizer(deny_brief_state_update)
            try:
                yield connection
            finally:
                connection.set_authorizer(None)

    # SQLite itself rejects the state write.  This works whether input and
    # invalidation use separate transactions today or one transaction after W3.
    with monkeypatch.context() as patch:
        patch.setattr(store, "get_connection", connection_with_state_fault)
        with pytest.raises((sqlite3.DatabaseError, PKVRuntimeError)):
            application.revise_outcome_claim(
                claim_id=prepared_outcome.claim.claim_id,
                statement="This changed claim has never been reviewed.",
                expected_revision=before.claims[0].revision,
            )
    assert denied_updates == [("outcome_briefs", "state")]

    after = KnowledgeApplication(application.config).get_outcome_bundle(topic_id=topic_id)
    assert after.brief is not None
    actual = (
        after.claims[0].statement,
        after.claims[0].revision,
        after.topic.revision,
        after.brief.revision,
        after.brief.visible_state,
    )
    expected = (
        before.claims[0].statement,
        before.claims[0].revision,
        before.topic.revision,
        before.brief.revision,
        before.brief.visible_state,
    )
    assert actual == expected, (
        "R-03: an aborted input/invalidation transaction must leave the whole old "
        "Claim, Topic revision, and approved brief intact"
    )


@pytest.mark.parametrize("action", ("approve", "return"))
def test_r04_unversioned_review_cannot_change_unreviewed_replacement(
    prepared_outcome: PreparedOutcome,
    action: str,
) -> None:
    """OC-07/OC-10b regression only: never silently review the latest revision."""

    reviewer = prepared_outcome.application
    topic_id = prepared_outcome.topic.topic_id
    reviewed = reviewer.get_outcome_research_brief(topic_id=topic_id)
    assert reviewed is not None and reviewed.revision == 1

    another_writer = KnowledgeApplication(reviewer.config)
    another_writer.revise_outcome_claim(
        claim_id=prepared_outcome.claim.claim_id,
        statement="A replacement claim the original reviewer has not seen.",
        expected_revision=prepared_outcome.claim.revision,
    )
    replacement = another_writer.render_outcome_research_brief(topic_id=topic_id)
    assert replacement.revision == reviewed.revision + 1
    assert replacement.content_sha256 != reviewed.content_sha256

    rejected: PKVRuntimeError | None = None
    review = (
        outcomes.approve_research_brief
        if action == "approve"
        else outcomes.return_research_brief
    )
    # Bind the existing public facade to the synthetic Application using the
    # established unit-test Kernel factory; never resolve the default Config.
    kernel = KnowledgeKernel._from_application(reviewer)
    try:
        review(topic_id=topic_id, kernel=kernel)
    except PKVRuntimeError as error:
        rejected = error

    observed = KnowledgeApplication(reviewer.config).get_outcome_research_brief(
        topic_id=topic_id
    )
    assert observed is not None
    assert observed.revision == replacement.revision
    assert observed.visible_state is OutcomeState.AWAITING_REVIEW, (
        f"R-04: the unversioned {action} request changed replacement revision "
        f"{observed.revision} to {observed.visible_state.value}; the caller read "
        f"revision {reviewed.revision}"
    )
    assert rejected is not None, "An unversioned review must fail closed, not silently succeed"


@pytest.mark.parametrize(
    "change_input",
    (False, True),
    ids=("same_input_control", "changed_input_regression"),
)
def test_r05_second_revision_orphan_does_not_block_recovery(
    prepared_outcome: PreparedOutcome,
    monkeypatch: pytest.MonkeyPatch,
    change_input: bool,
) -> None:
    """OC-04/OC-05: r2 must recover for both unchanged and subsequently edited input."""

    application = prepared_outcome.application
    topic_id = prepared_outcome.topic.topic_id
    original_path = (
        application.config.layout.outcomes_dir
        / prepared_outcome.brief.markdown_relative_path
    )
    original_bytes = original_path.read_bytes()
    second_claim = application.revise_outcome_claim(
        claim_id=prepared_outcome.claim.claim_id,
        statement="The second render input is published before its pointer.",
        expected_revision=prepared_outcome.claim.revision,
    )
    outcomes_dir = application.config.layout.outcomes_dir
    existing_files = set(outcomes_dir.rglob("*.md"))
    interrupted: list[int] = []

    def fail_before_pointer_commit(**kwargs: object) -> None:
        assert kwargs["revision"] == 2
        interrupted.append(2)
        raise PKVRuntimeError(
            ErrorCode.OUTCOME_STATE_CONFLICT,
            "W1 injected interruption before pointer commit",
            stage="outcome_brief",
            recoverable=True,
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            application.outcome_service._outcome_store,
            "upsert_rendered_brief",
            fail_before_pointer_commit,
        )
        with pytest.raises(PKVRuntimeError, match="W1 injected interruption"):
            application.render_outcome_research_brief(topic_id=topic_id)
    assert interrupted == [2]

    restarted = KnowledgeApplication(application.config)
    pointer = restarted.get_outcome_research_brief(topic_id=topic_id)
    assert pointer is not None
    assert pointer.revision == 1
    assert pointer.markdown_relative_path == prepared_outcome.brief.markdown_relative_path
    orphan_files = set(outcomes_dir.rglob("*.md")) - existing_files
    assert len(orphan_files) == 1
    orphan_path = orphan_files.pop()
    orphan_bytes = orphan_path.read_bytes()

    if change_input:
        restarted.revise_outcome_claim(
            claim_id=second_claim.claim_id,
            statement="The input changed after the interrupted second publication.",
            expected_revision=second_claim.revision,
        )
    recovered = restarted.render_outcome_research_brief(topic_id=topic_id)
    assert recovered.outcome_id == prepared_outcome.brief.outcome_id
    assert recovered.revision == 2
    assert recovered.visible_state is OutcomeState.AWAITING_REVIEW
    recovered_path = application.config.layout.outcomes_dir / recovered.markdown_relative_path
    assert recovered_path.is_file()
    assert (recovered_path != orphan_path) is change_input
    assert original_path.read_bytes() == original_bytes
    assert orphan_path.read_bytes() == orphan_bytes
    observed = KnowledgeApplication(application.config).get_outcome_research_brief(
        topic_id=topic_id
    )
    assert observed == recovered
