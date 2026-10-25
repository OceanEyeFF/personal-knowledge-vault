"""OC-10a: independent Outcome v2 DTOs and compatible major-1 SDK calls."""

from __future__ import annotations

from contextlib import closing
from dataclasses import FrozenInstanceError, replace
import hashlib
import inspect
from pathlib import Path
import sqlite3
from unittest.mock import Mock, call

import pytest

import pkv_kernel
from pkv_kernel import outcomes
from src.application import KnowledgeApplication
from src.outcomes.models import ResearchBriefRecord
from src.runtime.bootstrap import bootstrap_runtime
from src.storage.migration_manager import MigrationManager
from src.storage.sqlite_store import SQLiteStore, row_projection_sha256
from tests.unit.test_traceable_outcomes import _isolated_config, _seed_source_entry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIRMATION = {
    "outcome_id": "outcome-reviewed", "expected_revision": 2,
    "content_sha256": "a" * 64, "expected_input_revision": 7,
}
BRIEF_FIELDS = {
    "schema", "outcome_id", "topic_id", "outcome_type", "revision", "content_sha256",
    "state", "effective_state", "markdown_relative_path", "projection_state",
    "publication_id", "rendered_input_revision", "rendered_input_digest",
    "current_input_revision", "current_input_digest", "render_contract_version",
    "current_render_contract_version", "binding_state", "review_confirmation", "created_at", "updated_at",
}
V1_BRIEF_FIELDS = {
    "outcome_id", "topic_id", "outcome_type", "state", "effective_state", "revision",
    "markdown_relative_path", "content_sha256", "projection_state", "created_at", "updated_at",
}


def _brief() -> ResearchBriefRecord:
    return ResearchBriefRecord(
        outcome_id="outcome-reviewed", topic_id="topic-synthetic",
        state=outcomes.OutcomeState.AWAITING_REVIEW, revision=2,
        markdown_relative_path="topic-synthetic/brief.md", content_sha256="a" * 64,
        created_at="2026-10-25T00:00:00Z", updated_at="2026-10-25T00:00:00Z",
        binding_state="verified", publication_id="b" * 64,
        rendered_input_revision=7, rendered_input_digest="c" * 64,
        current_input_revision=7, current_input_digest="c" * 64,
        render_contract_version="research-brief.v2", current_render_contract_version="research-brief.v2",
    )


def _fake_kernel(**methods: object) -> Mock:
    kernel = Mock(spec=pkv_kernel.KnowledgeKernel)
    for name, value in methods.items():
        setattr(kernel, name, Mock(return_value=value))
    return kernel


def _fingerprint(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
    }


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("outcome_id", "../private"), ("outcome_id", "Outcome-a"), ("outcome_id", True),
        ("outcome_id", "C:/synthetic-secret"), ("outcome_id", "a" * 97),
        ("expected_revision", True), ("expected_revision", False), ("expected_revision", "2"),
        ("expected_revision", 0), ("expected_revision", -1), ("expected_revision", 2.0),
        ("expected_revision", 2**63),
        ("expected_input_revision", True), ("expected_input_revision", "7"),
        ("expected_input_revision", 0), ("content_sha256", "A" * 64),
        ("expected_input_revision", 2**63),
        ("content_sha256", "g" * 64), ("content_sha256", "a" * 63),
        ("content_sha256", True), ("content_sha256", " " + "a" * 64),
    ],
)
def test_confirmation_rejects_coercion_and_noncanonical_scalars(field, invalid):
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.ResearchBriefReviewConfirmation(**{**CONFIRMATION, field: invalid})
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_INVALID
    assert "synthetic-secret" not in str(rejected.value)


def test_confirmation_is_closed_immutable_and_serializes_only_four_fields():
    confirmation = outcomes.ResearchBriefReviewConfirmation(**CONFIRMATION)
    assert confirmation.to_dict() == CONFIRMATION
    with pytest.raises(FrozenInstanceError):
        confirmation.expected_revision = 3
    with pytest.raises(TypeError):
        outcomes.ResearchBriefReviewConfirmation(**CONFIRMATION, topic_id="topic-extra")
    copied = confirmation.to_dict()
    copied["expected_revision"] = 3
    assert confirmation.expected_revision == 2


def test_v2_request_rejects_extra_keywords_before_resolving_runtime(monkeypatch):
    resolve = Mock()
    monkeypatch.setattr(outcomes, "get_kernel", resolve)
    with pytest.raises(TypeError):
        outcomes.get_research_brief_v2(topic_id="topic-valid", private_root="synthetic")
    with pytest.raises(TypeError):
        outcomes.approve_research_brief_v2(
            confirmation=outcomes.ResearchBriefReviewConfirmation(**CONFIRMATION), latest=True,
        )
    resolve.assert_not_called()


@pytest.mark.parametrize(
    "selectors",
    ({}, {"topic_id": "topic-a", "outcome_id": "outcome-a"}, {"topic_id": True},
     {"outcome_id": "../secret"}, {"topic_id": ""}),
)
def test_bad_selectors_fail_before_resolving_any_kernel(monkeypatch, selectors):
    resolve = Mock(side_effect=AssertionError("invalid input must not resolve a runtime"))
    monkeypatch.setattr(outcomes, "get_kernel", resolve)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.get_research_brief_v2(**selectors)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_INVALID
    resolve.assert_not_called()


@pytest.mark.parametrize("method", [outcomes.get_outcome_v2, outcomes.render_research_brief_v2])
def test_invalid_topic_id_is_rejected_before_runtime(monkeypatch, method):
    resolve = Mock()
    monkeypatch.setattr(outcomes, "get_kernel", resolve)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        method(topic_id=False)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_INVALID
    resolve.assert_not_called()


@pytest.mark.parametrize("reason", [True, 1, None, "", "OTHER", "free text reason", {"reason": "other"}])
def test_return_rejects_untyped_or_free_text_reason_without_runtime(monkeypatch, reason):
    resolve = Mock()
    monkeypatch.setattr(outcomes, "get_kernel", resolve)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.return_research_brief_v2(
            confirmation=outcomes.ResearchBriefReviewConfirmation(**CONFIRMATION), reason_code=reason,
        )
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_INVALID
    resolve.assert_not_called()


@pytest.mark.parametrize("bad_confirmation", [None, CONFIRMATION, True])
def test_review_requires_the_closed_confirmation_dto(monkeypatch, bad_confirmation):
    resolve = Mock()
    monkeypatch.setattr(outcomes, "get_kernel", resolve)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.approve_research_brief_v2(confirmation=bad_confirmation)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_INVALID
    resolve.assert_not_called()


def test_review_revalidates_shape_even_if_frozen_object_was_bypassed(monkeypatch):
    confirmation = outcomes.ResearchBriefReviewConfirmation(**CONFIRMATION)
    object.__setattr__(confirmation, "expected_revision", True)
    resolve = Mock()
    monkeypatch.setattr(outcomes, "get_kernel", resolve)
    with pytest.raises(pkv_kernel.PKVRuntimeError):
        outcomes.approve_research_brief_v2(confirmation=confirmation)
    resolve.assert_not_called()


@pytest.mark.parametrize("action", ["approve", "return"])
def test_review_forwards_only_the_supplied_tuple_and_never_fetches_latest(action):
    kernel = _fake_kernel()
    method = Mock(side_effect=pkv_kernel.PKVRuntimeError(
        pkv_kernel.ErrorCode.OUTCOME_STATE_CONFLICT, "Synthetic stale confirmation",
    ))
    setattr(kernel, f"{action}_outcome_research_brief_v2", method)
    confirmation = outcomes.ResearchBriefReviewConfirmation(**CONFIRMATION)
    kwargs = {"confirmation": confirmation, "kernel": kernel}
    if action == "return":
        kwargs["reason_code"] = outcomes.ResearchBriefReturnReason.NEEDS_REVISION
    with pytest.raises(pkv_kernel.PKVRuntimeError):
        getattr(outcomes, f"{action}_research_brief_v2")(**kwargs)
    method.assert_called_once_with(**CONFIRMATION, **({"reason_code": "needs_revision"} if action == "return" else {}))
    assert kernel.mock_calls == [getattr(call, f"{action}_outcome_research_brief_v2")(
        **CONFIRMATION, **({"reason_code": "needs_revision"} if action == "return" else {}),
    )]


def test_brief_v2_is_independent_closed_and_drops_internal_metadata():
    internal = _brief()
    object.__setattr__(internal, "private_metadata", {"path": "C:/synthetic-secret", "source_body": "secret"})
    kernel = _fake_kernel(get_outcome_research_brief_v2=internal)
    result = outcomes.get_research_brief_v2(outcome_id=internal.outcome_id, kernel=kernel)
    assert type(result) is outcomes.ResearchBriefRecordV2
    assert not isinstance(result, outcomes.ResearchBriefRecord)
    assert set(result.to_dict()) == BRIEF_FIELDS
    assert result.review_confirmation.to_dict() == CONFIRMATION
    assert "synthetic-secret" not in str(result.to_dict())
    assert set(internal.to_dict()) == V1_BRIEF_FIELDS
    with pytest.raises(FrozenInstanceError):
        result.current_input_revision = 8


@pytest.mark.parametrize(
    "changes",
    [
        {"projection_state": "stale"}, {"projection_state": "unavailable"},
        {"effective_state": outcomes.OutcomeState.NEEDS_REVIEW}, {"current_input_revision": 8},
        {"current_input_digest": "d" * 64}, {"current_render_contract_version": "research-brief.v3"},
        {"binding_state": "legacy_unverified", "publication_id": None, "rendered_input_revision": None,
         "rendered_input_digest": None, "render_contract_version": None},
    ],
)
def test_unverifiable_brief_never_receives_a_review_confirmation(changes):
    internal = replace(_brief(), **changes)
    kernel = _fake_kernel(get_outcome_research_brief_v2=internal)
    result = outcomes.get_research_brief_v2(topic_id=internal.topic_id, kernel=kernel)
    assert result.review_confirmation is None


@pytest.mark.parametrize("state", [outcomes.OutcomeState.APPROVED, outcomes.OutcomeState.RETURNED])
def test_matching_terminal_review_state_retains_exact_confirmation_for_replay(state):
    kernel = _fake_kernel(get_outcome_research_brief_v2=replace(_brief(), state=state))
    result = outcomes.get_research_brief_v2(outcome_id="outcome-reviewed", kernel=kernel)
    assert result.review_confirmation.to_dict() == CONFIRMATION


@pytest.mark.parametrize("idempotent", [1, "true", None])
def test_review_result_never_coerces_nonboolean_idempotent(idempotent):
    kernel = _fake_kernel(get_outcome_research_brief_v2=_brief())
    brief = outcomes.get_research_brief_v2(outcome_id="outcome-reviewed", kernel=kernel)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.ResearchBriefReviewResultV2(brief, "f" * 64, idempotent)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_INVALID


@pytest.mark.parametrize("selector", [{"topic_id": "topic-missing"}, {"outcome_id": "outcome-missing"}])
def test_missing_valid_brief_selector_returns_none(selector):
    kernel = _fake_kernel(get_outcome_research_brief_v2=None)
    assert outcomes.get_research_brief_v2(**selector, kernel=kernel) is None


@pytest.mark.parametrize("action", ["approve", "return"])
def test_v1_review_preserves_signature_success_and_emits_migration_warning(action):
    method_name = f"{action}_research_brief"
    method = getattr(outcomes, method_name)
    assert tuple(inspect.signature(method).parameters) == ("topic_id", "kernel")
    sentinel = object()
    kernel = _fake_kernel(**{f"{action}_outcome_research_brief": sentinel})
    with pytest.warns(DeprecationWarning) as recorded:
        assert method(topic_id="topic-old", kernel=kernel) is sentinel
    assert len(recorded) == 1
    assert f"{method_name}_v2" in str(recorded[0].message)
    assert "API major 2" in str(recorded[0].message)


@pytest.mark.parametrize("required", [True, False, 0, 2, "1", 1.0, -1])
def test_handshake_rejects_wrong_or_coerced_api_major_without_runtime(monkeypatch, required):
    config = Mock(side_effect=AssertionError("metadata negotiation cannot open a data root"))
    monkeypatch.setattr(pkv_kernel, "get_config", config)
    monkeypatch.setattr(pkv_kernel, "get_kernel", config)
    with pytest.raises(pkv_kernel.KernelCompatibilityError):
        pkv_kernel.require_kernel_compatibility(required_api_major=required)
    with pytest.raises(pkv_kernel.KernelCompatibilityError):
        pkv_kernel.contracts.require_kernel_compatibility(pkv_kernel.__version__, required_api_major=required)
    config.assert_not_called()


@pytest.mark.parametrize("required", [None, 1])
def test_handshake_keeps_major1_compatibility_and_sdk_bounds(required):
    result = pkv_kernel.require_kernel_compatibility(required_api_major=required)
    assert result.api_version == "1.0.0"
    assert "kernel.traceable-outcomes.v1" in result.capabilities
    assert "kernel.embedding-rebuild.v2" not in result.capabilities
    assert pkv_kernel.contracts.require_kernel_compatibility(
        pkv_kernel.__version__, required_api_major=required,
    ) == result
    with pytest.raises(pkv_kernel.KernelCompatibilityError):
        pkv_kernel.require_kernel_compatibility(maximum_sdk_version="0.8.1", required_api_major=required)


@pytest.fixture
def public_scenario(tmp_path, monkeypatch):
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    app = KnowledgeApplication(config)
    forbidden = Mock(side_effect=AssertionError("Outcome APIs cannot construct Providers"))
    monkeypatch.setattr(KnowledgeApplication, "_create_embedder", forbidden)
    monkeypatch.setattr(KnowledgeApplication, "_create_deepseek_client", forbidden)
    knowledge_id = _seed_source_entry(app)
    kernel = pkv_kernel.get_kernel(config)
    topic = outcomes.create_topic(title="Public W3 inquiry", question="Which source supports this claim?", kernel=kernel)
    evidence = outcomes.attach_evidence(topic_id=topic.topic_id, locator=f"pkv://entries/{knowledge_id}", kernel=kernel)
    claim = outcomes.create_claim(topic_id=topic.topic_id, statement="A limited supported statement.", state="supported", kernel=kernel)
    claim = outcomes.link_claim_evidence(claim_id=claim.claim_id, evidence_id=evidence.evidence_id, polarity="support", kernel=kernel)
    brief = outcomes.render_research_brief_v2(topic_id=topic.topic_id, kernel=kernel)
    yield config, kernel, topic, claim, brief
    forbidden.assert_not_called()


def test_public_v2_reads_are_immutable_zero_write_and_zero_writer_lease(public_scenario, monkeypatch):
    config, kernel, topic, _, brief = public_scenario
    before = _fingerprint(config.layout.user_data_root)
    forbidden = Mock(side_effect=AssertionError("read must not initialize or acquire a writer lease"))
    monkeypatch.setattr(KnowledgeApplication, "_write_lease_scope", forbidden)
    monkeypatch.setattr(SQLiteStore, "initialize", forbidden)
    bundle = outcomes.get_outcome_v2(topic.topic_id, kernel=kernel)
    fetched = outcomes.get_research_brief_v2(outcome_id=brief.outcome_id, kernel=kernel)
    assert fetched == bundle.brief == brief
    assert outcomes.get_research_brief_v2(topic_id="topic-missing", kernel=kernel) is None
    assert outcomes.get_research_brief_v2(outcome_id="outcome-missing", kernel=kernel) is None
    with pytest.raises(pkv_kernel.PKVRuntimeError) as missing:
        outcomes.get_outcome_v2("topic-missing", kernel=kernel)
    assert missing.value.code is pkv_kernel.ErrorCode.OUTCOME_NOT_FOUND
    assert set(bundle.to_dict()) == {"schema", "topic", "evidence", "claims", "brief"}
    assert bundle.topic["outcome_id"] == brief.outcome_id
    assert bundle.topic["input_revision"] == brief.current_input_revision
    old_payload = outcomes.get_outcome(topic.topic_id, kernel=kernel).to_dict()
    assert set(old_payload) == {"topic", "evidence", "claims", "brief", "requires_review"}
    assert set(old_payload["topic"]) == {"topic_id", "title", "question", "state", "revision", "created_at", "updated_at"}
    assert set(old_payload["brief"]) == V1_BRIEF_FIELDS
    with pytest.raises(pkv_kernel.PKVRuntimeError):
        outcomes.OutcomeBundleV2(
            topic={**bundle.topic, "private_metadata": "C:/synthetic-secret"},
            evidence=bundle.evidence, claims=bundle.claims, brief=bundle.brief,
        )
    with pytest.raises(TypeError):
        bundle.topic["title"] = "mutated"
    copied = bundle.to_dict()
    copied["topic"]["title"] = "mutated"
    assert bundle.topic["title"] == topic.title
    assert "source-body-must-not-appear-in-brief" not in str(copied)
    forbidden.assert_not_called()
    assert _fingerprint(config.layout.user_data_root) == before


def test_public_v2_rejects_old_tuple_and_replays_exact_review_after_reopen(public_scenario):
    config, kernel, topic, claim, first = public_scenario
    outcomes.revise_claim(claim_id=claim.claim_id, statement="A revised supported statement.", expected_revision=claim.revision, kernel=kernel)
    current = outcomes.render_research_brief_v2(topic_id=topic.topic_id, kernel=kernel)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.approve_research_brief_v2(confirmation=first.review_confirmation, kernel=kernel)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_STATE_CONFLICT
    accepted = outcomes.approve_research_brief_v2(confirmation=current.review_confirmation, kernel=kernel)
    replayed = outcomes.approve_research_brief_v2(
        confirmation=current.review_confirmation, kernel=pkv_kernel.get_kernel(config),
    )
    assert not accepted.idempotent and replayed.idempotent
    assert accepted.review_receipt_id == replayed.review_receipt_id
    returned = outcomes.return_research_brief_v2(
        confirmation=current.review_confirmation, reason_code="needs_revision", kernel=kernel,
    )
    repeated = outcomes.return_research_brief_v2(
        confirmation=current.review_confirmation, reason_code="needs_revision", kernel=pkv_kernel.get_kernel(config),
    )
    assert not returned.idempotent and repeated.idempotent
    assert returned.review_receipt_id == repeated.review_receipt_id
    assert returned.review_receipt_id != accepted.review_receipt_id
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.approve_research_brief_v2(confirmation=current.review_confirmation, kernel=kernel)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_STATE_CONFLICT


def test_public_v2_refuses_review_of_migrated_legacy_brief(tmp_path, monkeypatch):
    """OC-03: a real 014 upgrade cannot manufacture a legacy review binding."""

    from src.runtime.write_lease import write_lease_scope
    from src.utils.text_utils import TextProcessor

    config = _isolated_config(tmp_path)
    config.ensure_dirs()
    manager = MigrationManager(config.db_path, PROJECT_ROOT / "scripts" / "migrations")
    for migration in sorted((PROJECT_ROOT / "scripts" / "migrations").glob("*.sql")):
        if migration.name < "014_":
            manager.apply_migration(migration, auto_backup=False)
    assert manager.get_current_version() == "1.2.7"

    with write_lease_scope(config.layout):
        TextProcessor(runtime_config=config, initialize_cache=True)
    application = KnowledgeApplication(config)
    knowledge_id = _seed_source_entry(application)
    source_sha = row_projection_sha256(
        application.sqlite_store.query_by_id(knowledge_id),
        application.sqlite_store.get_chunks_by_knowledge_id(knowledge_id),
    )

    # The pre-014 row and its current Markdown are synthetic historical facts.
    # No missing source or projection is needed to make this binding unverified.
    historical_bytes = b"# Synthetic approved legacy brief\n"
    historical_sha = hashlib.sha256(historical_bytes).hexdigest()
    projection = config.layout.outcomes_dir / "legacy-approved.md"
    projection.parent.mkdir(parents=True, exist_ok=True)
    projection.write_bytes(historical_bytes)
    with closing(sqlite3.connect(config.db_path)) as connection:
        connection.execute(
            "INSERT INTO outcome_topics(topic_id,title,question,state,revision) "
            "VALUES('topic-legacy','Synthetic legacy inquiry','Which version was reviewed?','active',3)"
        )
        connection.execute(
            "INSERT INTO outcome_briefs(outcome_id,topic_id,outcome_type,state,revision,"
            "markdown_relative_path,content_sha256) "
            "VALUES('outcome-legacy','topic-legacy','research_brief','approved',2,'legacy-approved.md',?)",
            (historical_sha,),
        )
        connection.execute(
            "INSERT INTO outcome_evidence(evidence_id,topic_id,ordinal,locator,locator_kind,knowledge_id,"
            "source_revision_sha256,acquisition_method,retrieval_status,recorded_state) "
            "VALUES('evidence-legacy','topic-legacy',1,?,'entry',?,?,'user_selected','not_requested','current')",
            (f"pkv://entries/{knowledge_id}", knowledge_id, source_sha),
        )
        connection.execute(
            "INSERT INTO outcome_claims(claim_id,topic_id,ordinal,statement,state) "
            "VALUES('claim-legacy','topic-legacy',1,'A synthetic supported historical claim.','supported')"
        )
        connection.execute(
            "INSERT INTO outcome_claim_evidence(claim_id,evidence_id,polarity) "
            "VALUES('claim-legacy','evidence-legacy','support')"
        )
        connection.commit()

    assert manager.apply_all_pending(auto_backup=False) == 1
    assert manager.get_current_version() == "1.2.8"
    assert projection.read_bytes() == historical_bytes
    database_before = config.db_path.read_bytes()
    markdown_before = {path.relative_to(config.layout.user_data_root): path.read_bytes()
                       for path in config.layout.user_data_root.rglob("*.md")}
    tree_before = _fingerprint(config.layout.user_data_root)
    forbidden = Mock(side_effect=AssertionError("public review/read cannot initialize, migrate or call Providers"))
    monkeypatch.setattr(MigrationManager, "apply_all_pending", forbidden)
    monkeypatch.setattr(MigrationManager, "apply_migration", forbidden)
    monkeypatch.setattr(SQLiteStore, "initialize", forbidden)
    monkeypatch.setattr(pkv_kernel, "bootstrap_kernel", forbidden)
    monkeypatch.setattr(KnowledgeApplication, "_create_embedder", forbidden)
    monkeypatch.setattr(KnowledgeApplication, "_create_deepseek_client", forbidden)
    kernel = pkv_kernel.get_kernel(config)
    with monkeypatch.context() as read_scope:
        read_scope.setattr(KnowledgeApplication, "_write_lease_scope", forbidden)
        bundle = outcomes.get_outcome_v2("topic-legacy", kernel=kernel)
        brief = outcomes.get_research_brief_v2(outcome_id="outcome-legacy", kernel=kernel)

    assert brief == bundle.brief
    assert len(bundle.evidence) == len(bundle.claims) == 1
    assert bundle.evidence[0].recorded_state is outcomes.EvidenceState.CURRENT
    assert bundle.claims[0].state is outcomes.ClaimState.SUPPORTED
    assert tuple(link.to_dict() for link in bundle.claims[0].evidence_links) == (
        {"evidence_id": "evidence-legacy", "polarity": "support"},
    )
    assert brief.state is outcomes.OutcomeState.APPROVED
    assert brief.effective_state is outcomes.OutcomeState.NEEDS_REVIEW
    assert brief.projection_state == "current"
    assert brief.binding_state == "legacy_unverified"
    assert brief.publication_id is None
    assert brief.rendered_input_revision is None
    assert brief.rendered_input_digest is None
    assert brief.review_confirmation is None
    assert brief.to_dict()["review_confirmation"] is None
    assert brief.revision == 2 and brief.content_sha256 == historical_sha
    assert _fingerprint(config.layout.user_data_root) == tree_before

    # A well-shaped tuple made from the old pointer plus current input revision
    # is still untrusted input; the SDK must not treat it as historical approval.
    supplied = outcomes.ResearchBriefReviewConfirmation(
        outcome_id="outcome-legacy", expected_revision=2,
        content_sha256=historical_sha, expected_input_revision=brief.current_input_revision,
    )
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        outcomes.approve_research_brief_v2(confirmation=supplied, kernel=kernel)
    assert rejected.value.code is pkv_kernel.ErrorCode.OUTCOME_APPROVAL_BLOCKED
    assert rejected.value.stage == "outcome_review"
    forbidden.assert_not_called()
    assert config.db_path.read_bytes() == database_before
    assert {path.relative_to(config.layout.user_data_root): path.read_bytes()
            for path in config.layout.user_data_root.rglob("*.md")} == markdown_before
    with closing(sqlite3.connect(f"{config.db_path.as_uri()}?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT count(*) FROM outcome_review_receipts").fetchone()[0] == 0
        assert connection.execute(
            "SELECT state,revision,binding_state FROM outcome_briefs WHERE outcome_id='outcome-legacy'"
        ).fetchone() == ("approved", 2, "legacy_unverified")


@pytest.mark.parametrize("operation", ["bundle", "brief"])
def test_public_v2_read_rejects_actual_013_schema_without_upgrade_or_write(tmp_path, monkeypatch, operation):
    config = _isolated_config(tmp_path)
    config.ensure_dirs()
    manager = MigrationManager(config.db_path, PROJECT_ROOT / "scripts" / "migrations")
    for migration in sorted((PROJECT_ROOT / "scripts" / "migrations").glob("*.sql")):
        if migration.name < "014_":
            manager.apply_migration(migration, auto_backup=False)
    kernel = pkv_kernel.get_kernel(config)
    before = _fingerprint(config.layout.user_data_root)
    forbidden = Mock(side_effect=AssertionError("read cannot migrate or lease"))
    monkeypatch.setattr(KnowledgeApplication, "_write_lease_scope", forbidden)
    monkeypatch.setattr(MigrationManager, "apply_all_pending", forbidden)
    monkeypatch.setattr(SQLiteStore, "initialize", forbidden)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        if operation == "bundle":
            outcomes.get_outcome_v2("topic-absent", kernel=kernel)
        else:
            outcomes.get_research_brief_v2(outcome_id="outcome-absent", kernel=kernel)
    assert rejected.value.code is pkv_kernel.ErrorCode.DATABASE_UPGRADE_REQUIRED
    forbidden.assert_not_called()
    assert _fingerprint(config.layout.user_data_root) == before


@pytest.mark.parametrize("operation", ["bundle", "brief"])
def test_direct_public_v2_read_rejects_forged_latest_schema_without_bootstrap(tmp_path, monkeypatch, operation):
    """A version marker cannot replace structural CHECK validation on SDK reads."""

    config = _isolated_config(tmp_path)
    config.ensure_dirs()
    MigrationManager(config.db_path, PROJECT_ROOT / "scripts" / "migrations").apply_all_pending(auto_backup=False)
    with sqlite3.connect(config.db_path) as connection:
        original = connection.execute("SELECT sql FROM sqlite_master WHERE name='outcome_topics'").fetchone()[0]
        missing_check = original.replace("CHECK(typeof(input_revision) = 'integer' AND input_revision >= 1)", "")
        assert missing_check != original
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute("UPDATE sqlite_master SET sql=? WHERE name='outcome_topics'", (missing_check,))
        connection.execute("PRAGMA writable_schema=OFF")

    # Construct only the explicit-config graph: no bootstrap/inspection may
    # pre-reject the fixture on behalf of the v2 read boundary under test.
    kernel = pkv_kernel.get_kernel(config)
    before = _fingerprint(config.layout.user_data_root)
    forbidden = Mock(side_effect=AssertionError("SDK read cannot bootstrap, migrate or lease"))
    monkeypatch.setattr(KnowledgeApplication, "_write_lease_scope", forbidden)
    monkeypatch.setattr(MigrationManager, "apply_all_pending", forbidden)
    monkeypatch.setattr(SQLiteStore, "initialize", forbidden)
    monkeypatch.setattr(pkv_kernel, "bootstrap_kernel", forbidden)
    with pytest.raises(pkv_kernel.PKVRuntimeError) as rejected:
        if operation == "bundle":
            outcomes.get_outcome_v2("topic-absent", kernel=kernel)
        else:
            outcomes.get_research_brief_v2(outcome_id="outcome-absent", kernel=kernel)
    assert rejected.value.code is pkv_kernel.ErrorCode.DATABASE_SCHEMA_DRIFT
    forbidden.assert_not_called()
    assert _fingerprint(config.layout.user_data_root) == before
