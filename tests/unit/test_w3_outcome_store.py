"""W3 transaction and identity oracles using synthetic, real SQLite databases."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event

import pytest

from src.outcomes.identity import (MAX_REVISION, RENDER_CONTRACT_VERSION, outcome_input_digest,
                                   outcome_input_payload, outcome_publication_id, outcome_review_receipt_id)
from src.outcomes.models import (ClaimState, ClaimEvidencePolarity, EvidenceAcquisitionMethod,
                                 EvidenceLocator, EvidenceLocatorKind, EvidenceState, OutcomeState, TopicState)
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.write_lease import write_lease_scope
from src.storage.outcome_store import OutcomeStore
from tests.unit.test_traceable_outcomes import _isolated_config


def publish(store, *, previous=None):
    topic, _, _, _ = store.get_bundle_rows("topic_test")
    revision = 1 if previous is None else previous.revision + 1
    publication = outcome_publication_id(outcome_id=topic.outcome_id, candidate_revision=revision, input_digest=topic.input_digest)
    return store.upsert_rendered_brief(
        outcome_id=topic.outcome_id, topic_id=topic.topic_id, revision=revision,
        markdown_relative_path=f"topic-{topic.topic_id}/{topic.outcome_id}-r{revision:04d}-p{publication}.md",
        content_sha256="c" * 64, expected_previous_revision=previous.revision if previous else None,
        expected_previous_pointer=previous, expected_input_revision=topic.input_revision,
        expected_input_digest=topic.input_digest, render_contract_version=topic.render_contract_version,
        publication_id=publication,
    )


@pytest.fixture
def store(tmp_path):
    config = _isolated_config(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    store = OutcomeStore(config.db_path, layout=config.layout)
    with write_lease_scope(store.layout):
        store.create_topic(topic_id="topic_test", title="A topic", question="A question?")
        store.create_evidence(evidence_id="evidence_a", topic_id="topic_test", locator=EvidenceLocator.parse("pkv://entries/1"),
                              source_revision_sha256="a"*64, acquisition_method=EvidenceAcquisitionMethod.USER_SELECTED,
                              retrieval_status="not_requested", limitation_codes=())
        store.create_claim(claim_id="claim_a", topic_id="topic_test", statement="A statement", state=ClaimState.SUPPORTED,
                           limitation_codes=("limited",))
        store.link_claim_evidence(claim_id="claim_a", evidence_id="evidence_a", polarity=ClaimEvidencePolarity.SUPPORT)
        publish(store)
        store.set_brief_state(topic_id="topic_test", state=OutcomeState.APPROVED)
    return store


def mutate(store, kind):
    if kind == "topic":
        return store.update_topic(topic_id="topic_test", title="A revised topic", expected_revision=1)
    if kind == "evidence_add":
        return store.create_evidence(evidence_id="evidence_b", topic_id="topic_test", locator=EvidenceLocator.parse("pkv://entries/2"),
                                    source_revision_sha256="b"*64, acquisition_method=EvidenceAcquisitionMethod.USER_SELECTED,
                                    retrieval_status="not_requested", limitation_codes=())
    if kind == "evidence_delete":
        return store.delete_evidence("evidence_a")
    if kind == "evidence_state":
        return store.update_evidence_recorded_states({"evidence_a": EvidenceState.STALE})
    if kind == "claim_add":
        return store.create_claim(claim_id="claim_b", topic_id="topic_test", statement="Another statement", state=ClaimState.DRAFT, limitation_codes=())
    if kind == "claim_update":
        return store.update_claim(claim_id="claim_a", statement="A revised statement", state=None, limitation_codes=None, expected_revision=1)
    if kind == "edge_change":
        return store.link_claim_evidence(claim_id="claim_a", evidence_id="evidence_a", polarity=ClaimEvidencePolarity.COUNTER)
    if kind == "edge_delete":
        return store.unlink_claim_evidence(claim_id="claim_a", evidence_id="evidence_a")
    raise AssertionError(kind)


MUTATIONS = ("topic", "evidence_add", "evidence_delete", "evidence_state", "claim_add", "claim_update", "edge_change", "edge_delete")


@pytest.mark.parametrize("kind", MUTATIONS)
def test_input_mutation_advances_once_and_invalidates_in_one_commit(store, kind):
    before = store.get_bundle_rows("topic_test")
    with write_lease_scope(store.layout):
        mutate(store, kind)
    after = OutcomeStore(store.db_path, layout=store.layout).get_bundle_rows("topic_test")
    assert after[0].input_revision == before[0].input_revision + 1
    assert after[0].input_digest != before[0].input_digest
    assert after[0].outcome_id == before[0].outcome_id
    assert after[0].revision == before[0].revision + (kind == "topic")
    assert after[3].state is OutcomeState.NEEDS_REVIEW
    assert after[3].revision == before[3].revision
    assert after[0].input_digest == outcome_input_digest(topic=after[0], evidence=after[1], claims=after[2])
    if kind == "evidence_delete":
        assert after[1] == () and after[2][0].evidence_links == ()


@pytest.mark.parametrize("kind", MUTATIONS)
@pytest.mark.parametrize("cut", ["input_revision", "input_digest", "brief_state"])
def test_sql_fault_rolls_back_input_identity_and_invalidation(store, monkeypatch, kind, cut):
    before = store.get_bundle_rows("topic_test")
    original = store.get_connection
    denied = []

    @contextmanager
    def interrupted():
        with original() as connection:
            def authorize(action, table, column, *_):
                target = ("outcome_briefs", "state") if cut == "brief_state" else ("outcome_topics", cut)
                if action == sqlite3.SQLITE_UPDATE and (table, column) == target:
                    denied.append(target)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            yield connection
    monkeypatch.setattr(store, "get_connection", interrupted)
    with write_lease_scope(store.layout), pytest.raises(sqlite3.DatabaseError):
        mutate(store, kind)
    assert denied
    fresh = OutcomeStore(store.db_path, layout=store.layout)
    assert fresh.get_bundle_rows("topic_test") == before


def test_normalized_noops_leave_all_identities_and_approval_unchanged(store):
    before = store.get_bundle_rows("topic_test")
    with write_lease_scope(store.layout):
        store.update_topic(topic_id="topic_test", title=" A   topic ", question=" A question? ", state=TopicState.ACTIVE)
        store.update_claim(claim_id="claim_a", statement=" A   statement ", state=ClaimState.SUPPORTED,
                           limitation_codes=("limited", "limited"), expected_revision=1)
        store.link_claim_evidence(claim_id="claim_a", evidence_id="evidence_a", polarity=ClaimEvidencePolarity.SUPPORT)
        assert store.update_evidence_recorded_states({"evidence_a": EvidenceState.CURRENT}) == ()
    assert store.get_bundle_rows("topic_test") == before
    with write_lease_scope(store.layout), pytest.raises(PKVRuntimeError) as error:
        store.update_claim(claim_id="claim_a", statement="A statement", state=None, limitation_codes=None, expected_revision=2)
    assert error.value.code is ErrorCode.OUTCOME_STATE_CONFLICT


def test_batch_state_change_advances_topic_only_once(store):
    with write_lease_scope(store.layout):
        mutate(store, "evidence_add")
        before = store.get_topic("topic_test")
        store.update_evidence_recorded_states({"evidence_a": EvidenceState.STALE, "evidence_b": EvidenceState.UNAVAILABLE})
    assert store.get_topic("topic_test").input_revision == before.input_revision + 1


@pytest.mark.parametrize("field,value", [("markdown_relative_path", "different.md"), ("content_sha256", "d"*64), ("publication_id", "e"*64)])
def test_pointer_cas_compares_complete_previous_tuple(store, field, value):
    before = store.get_bundle_rows("topic_test")
    stale = replace(before[3], **{field: value})
    with write_lease_scope(store.layout), pytest.raises(PKVRuntimeError) as error:
        publish(store, previous=stale)
    assert error.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert store.get_bundle_rows("topic_test") == before


def review_args(brief):
    return dict(outcome_id=brief.outcome_id, expected_revision=brief.revision,
                content_sha256=brief.content_sha256, expected_input_revision=brief.rendered_input_revision)


def test_review_receipt_replay_is_bound_to_current_state_and_tuple(store):
    with write_lease_scope(store.layout):
        brief = publish(store, previous=store.get_brief_by_topic("topic_test"))
        before = store.get_topic("topic_test")
        approved = store.review_brief(**review_args(brief), action="approve", reason_code=None)
        replay = store.review_brief(**review_args(brief), action="approve", reason_code=None)
        assert not approved.idempotent and replay.idempotent and replay.receipt_id == approved.receipt_id
        returned = store.review_brief(**review_args(brief), action="return", reason_code="needs_revision")
        assert not returned.idempotent
        assert store.review_brief(**review_args(brief), action="return", reason_code="needs_revision").idempotent
        for action, reason in (("approve", None), ("return", "other")):
            with pytest.raises(PKVRuntimeError) as error:
                store.review_brief(**review_args(brief), action=action, reason_code=reason)
            assert error.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert store.get_brief(brief.outcome_id).state is OutcomeState.RETURNED
    assert store.get_topic("topic_test") == before
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM outcome_review_receipts").fetchone()[0] == 2


@pytest.mark.parametrize("cut", ["state", "receipt"])
def test_review_receipt_and_state_are_atomic(store, monkeypatch, cut):
    with write_lease_scope(store.layout):
        brief = publish(store, previous=store.get_brief_by_topic("topic_test"))
    before = store.get_bundle_rows("topic_test")
    original = store.get_connection
    denied = []
    @contextmanager
    def interrupted():
        with original() as connection:
            def authorize(action, table, column, *_):
                match = ((action == sqlite3.SQLITE_UPDATE and table == "outcome_briefs" and column == "state") if cut == "state"
                         else (action == sqlite3.SQLITE_INSERT and table == "outcome_review_receipts"))
                if match:
                    denied.append(cut)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            yield connection
    monkeypatch.setattr(store, "get_connection", interrupted)
    with write_lease_scope(store.layout), pytest.raises(sqlite3.DatabaseError):
        store.review_brief(**review_args(brief), action="approve", reason_code=None)
    assert denied
    assert OutcomeStore(store.db_path, layout=store.layout).get_bundle_rows("topic_test") == before
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM outcome_review_receipts").fetchone()[0] == 0


def test_unreceipted_legacy_approval_is_not_v2_idempotency(store):
    brief = store.get_brief_by_topic("topic_test")
    with write_lease_scope(store.layout), pytest.raises(PKVRuntimeError) as error:
        store.review_brief(**review_args(brief), action="approve", reason_code=None)
    assert error.value.code is ErrorCode.OUTCOME_STATE_CONFLICT


def test_read_bundle_uses_one_snapshot_across_independent_writer(store, monkeypatch):
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    before = store.get_bundle_rows("topic_test")
    captured, committed = Event(), Event()
    committed_before_resume = []
    original = store.get_connection
    @contextmanager
    def paused_reader():
        with original() as connection:
            def trace(sql):
                if "SELECT * FROM outcome_evidence" in sql and not captured.is_set():
                    captured.set()
                    committed_before_resume.append(committed.wait(10))
            connection.set_trace_callback(trace)
            yield connection
    monkeypatch.setattr(store, "get_connection", paused_reader)
    def writer():
        assert captured.wait(10)
        other = OutcomeStore(store.db_path, layout=store.layout)
        try:
            with write_lease_scope(other.layout):
                mutate(other, "claim_update")
        finally:
            committed.set()
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(writer)
        observed = store.get_bundle_rows("topic_test")
        task.result(timeout=15)
    # sqlite trace callbacks swallow exceptions; assert the barrier outside it.
    assert committed_before_resume == [True]
    assert observed == before
    after = OutcomeStore(store.db_path, layout=store.layout).get_bundle_rows("topic_test")
    assert after[0].input_revision == before[0].input_revision + 1
    assert after[2][0].statement == "A revised statement"
    assert after[3].state is OutcomeState.NEEDS_REVIEW


def test_revision_overflow_rejects_and_rolls_back_mutation(store):
    with write_lease_scope(store.layout), store.get_connection() as connection:
        connection.execute("UPDATE outcome_topics SET input_revision=?", (MAX_REVISION,))
        topic, evidence, claims, _ = store._bundle_from_connection(connection, "topic_test", verify=False)
        connection.execute("UPDATE outcome_topics SET input_digest=?", (outcome_input_digest(topic=topic, evidence=evidence, claims=claims),))
    before = store.get_bundle_rows("topic_test")
    with write_lease_scope(store.layout), pytest.raises(ValueError):
        mutate(store, "claim_update")
    assert store.get_bundle_rows("topic_test") == before


def test_canonical_identities_match_frozen_domains_and_keep_v1_shape(store):
    topic, evidence, claims, brief = store.get_bundle_rows("topic_test")
    payload = outcome_input_payload(topic=topic, evidence=evidence, claims=claims)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert topic.input_digest == hashlib.sha256(b"pkv.outcome.input.v1\0" + canonical).hexdigest()
    assert set(payload["evidence"][0]) == {"evidence_id", "ordinal", "locator", "locator_kind", "source_revision_sha256", "acquisition_method", "retrieval_status", "recorded_state", "limitation_codes"}
    assert set(topic.to_dict()) == {"topic_id", "title", "question", "state", "revision", "created_at", "updated_at"}
    assert "publication_id" not in brief.to_dict() and "binding_state" not in brief.to_dict()
    raw = payload["topic"]
    for bad in (True, 1.0, 0, MAX_REVISION + 1):
        with pytest.raises(ValueError):
            outcome_input_digest(topic=raw, evidence=payload["evidence"], claims=payload["claims"], input_revision=bad, render_contract_version=RENDER_CONTRACT_VERSION)
    with pytest.raises(ValueError):
        outcome_input_digest(topic={**raw, "created_at": "forbidden"}, evidence=(), claims=(), input_revision=1, render_contract_version=RENDER_CONTRACT_VERSION)


@pytest.mark.parametrize("kind", MUTATIONS)
def test_mutation_never_resigns_a_corrupted_existing_input(store, kind):
    with write_lease_scope(store.layout), store.get_connection() as connection:
        connection.execute("UPDATE outcome_topics SET input_digest=?", ("f" * 64,))
    with sqlite3.connect(store.db_path) as connection:
        before = tuple(connection.iterdump())
    with write_lease_scope(store.layout), pytest.raises(PKVRuntimeError) as error:
        mutate(store, kind)
    assert error.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    with sqlite3.connect(store.db_path) as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.parametrize("change", ["input", "pointer"])
def test_review_rechecks_current_input_and_pointer(store, change):
    with write_lease_scope(store.layout):
        brief = publish(store, previous=store.get_brief_by_topic("topic_test"))
        if change == "input":
            mutate(store, "claim_update")
        else:
            publish(store, previous=brief)
        before = store.get_bundle_rows("topic_test")
        with pytest.raises(PKVRuntimeError) as error:
            store.review_brief(**review_args(brief), action="approve", reason_code=None)
    assert error.value.code is ErrorCode.OUTCOME_STATE_CONFLICT
    assert store.get_bundle_rows("topic_test") == before


def test_roundtrip_input_change_keeps_new_identity_and_review_history(store):
    with write_lease_scope(store.layout):
        brief = publish(store, previous=store.get_brief_by_topic("topic_test"))
        original = store.get_topic("topic_test")
        receipt = store.review_brief(**review_args(brief), action="approve", reason_code=None)
        store.update_topic(topic_id="topic_test", title="A different title")
        store.update_topic(topic_id="topic_test", title=original.title)
        current = store.get_topic("topic_test")
        assert current.input_revision == original.input_revision + 2
        assert current.input_digest != original.input_digest
        next_brief = publish(store, previous=brief)
        assert next_brief.outcome_id == brief.outcome_id
        assert next_brief.publication_id != brief.publication_id
        with store.get_connection() as connection:
            assert connection.execute("SELECT receipt_id FROM outcome_review_receipts").fetchone()[0] == receipt.receipt_id
        with pytest.raises(PKVRuntimeError):
            store.review_brief(**review_args(brief), action="approve", reason_code=None)


def test_identity_frozen_material_and_sorting_without_model_dto():
    topic = dict(topic_id="topic_fixed", outcome_id="outcome-fixed", title="研究  主题", question="问题?", state="active", revision=2)
    evidence = [dict(evidence_id=name, ordinal=ordinal, locator=f"pkv://entries/{ordinal}", locator_kind="entry",
                     source_revision_sha256="a"*64, acquisition_method="user_selected", retrieval_status="not_requested",
                     recorded_state="current", limitation_codes=["z_code", "a_code", "a_code"])
                for name, ordinal in (("evidence_z", 1), ("evidence_a", 2))]
    claims = [dict(claim_id="claim_fixed", ordinal=1, statement="一句  陈述", state="supported", revision=1,
                   limitation_codes=[], evidence_links=[dict(evidence_id="evidence_z", polarity="support"),
                                                        dict(evidence_id="evidence_a", polarity="counter")])]
    expected = dict(input_revision=3, render_contract_version=RENDER_CONTRACT_VERSION,
                    topic={**topic, "title": "研究 主题"},
                    evidence=[{**row, "limitation_codes": ["a_code", "z_code"]} for row in evidence],
                    claims=[{**claims[0], "statement": "一句 陈述", "evidence_links": list(reversed(claims[0]["evidence_links"]))}])
    digest = outcome_input_digest(topic=topic, evidence=reversed(evidence), claims=claims, input_revision=3,
                                  render_contract_version=RENDER_CONTRACT_VERSION)
    encode = lambda data: json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert digest == hashlib.sha256(b"pkv.outcome.input.v1\0" + encode(expected)).hexdigest()
    publication_payload = dict(outcome_id="outcome-fixed", candidate_revision=4, input_digest=digest)
    assert outcome_publication_id(**publication_payload) == hashlib.sha256(b"pkv.outcome.publication.v1\0" + encode(publication_payload)).hexdigest()
    receipt_payload = dict(outcome_id="outcome-fixed", expected_revision=4, content_sha256="b"*64,
                           expected_input_revision=3, action="return", reason_code="needs_revision")
    assert outcome_review_receipt_id(**receipt_payload) == hashlib.sha256(b"pkv.outcome.review.v1\0" + encode(receipt_payload)).hexdigest()
    for bad_codes in (None, "a_code", {"a_code": True}):
        with pytest.raises(ValueError):
            outcome_input_digest(topic=topic, evidence=[{**evidence[0], "limitation_codes": bad_codes}], claims=(), input_revision=3,
                                 render_contract_version=RENDER_CONTRACT_VERSION)


def test_direct_store_rejects_forged_schema_without_check_and_does_not_write(store, monkeypatch):
    """A 1.2.8 label and matching columns do not substitute for constraints."""
    with write_lease_scope(store.layout), sqlite3.connect(store.db_path) as connection:
        connection.execute("PRAGMA writable_schema=ON")
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE name='outcome_topics'").fetchone()[0]
        constraint = "CHECK(typeof(input_revision) = 'integer' AND input_revision >= 1)"
        assert constraint in sql
        connection.execute("UPDATE sqlite_master SET sql=? WHERE name='outcome_topics'", (sql.replace(constraint, ""),))
        connection.execute("PRAGMA writable_schema=OFF")
    before = store.db_path.read_bytes()
    direct = OutcomeStore(store.db_path, layout=store.layout)
    monkeypatch.setattr(direct, "_require_write", lambda: pytest.fail("read must not acquire a writer"))
    with pytest.raises(PKVRuntimeError) as error:
        direct.get_bundle_rows("topic_test")
    assert error.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    assert store.db_path.read_bytes() == before


@pytest.mark.parametrize("corruption", [
    "UPDATE outcome_evidence SET locator_kind='chunk',chunk_id=1",
    "UPDATE outcome_evidence SET knowledge_id=2",
    "UPDATE outcome_claims SET revision=1.5",
    "UPDATE outcome_evidence SET ordinal=1.5",
    "UPDATE outcome_evidence SET limitation_codes='null'",
    "UPDATE outcome_claims SET limitation_codes='{\"limited\":true}'",
], ids=["locator_kind", "derived_id", "real_revision", "real_ordinal", "json_null", "json_object"])
def test_raw_persisted_input_cannot_be_hidden_by_model_normalization(store, corruption):
    with write_lease_scope(store.layout), sqlite3.connect(store.db_path) as connection:
        connection.execute(corruption)  # All are legal under the retained 013 CHECKs.
    before = store.db_path.read_bytes()
    with pytest.raises(PKVRuntimeError) as error:
        store.get_bundle_rows("topic_test")
    assert error.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    with write_lease_scope(store.layout), pytest.raises(PKVRuntimeError) as error:
        mutate(store, "claim_update")
    assert error.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT
    assert store.db_path.read_bytes() == before


def test_current_input_cannot_be_substituted_for_old_publication_confirmation(store):
    with write_lease_scope(store.layout):
        brief = publish(store, previous=store.get_brief_by_topic("topic_test"))
        mutate(store, "claim_update")
        confirmation = review_args(brief)
        confirmation["expected_input_revision"] = store.get_topic("topic_test").input_revision
        with pytest.raises(PKVRuntimeError) as error:
            store.review_brief(**confirmation, action="approve", reason_code=None)
    assert error.value.code is ErrorCode.OUTCOME_STATE_CONFLICT


def test_new_inconsistent_locator_rolls_back_without_signing_it(store):
    before = store.get_bundle_rows("topic_test")
    inconsistent = EvidenceLocator(value="pkv://entries/2", kind=EvidenceLocatorKind.CHUNK, knowledge_id=2, chunk_id=1)
    with write_lease_scope(store.layout), pytest.raises(PKVRuntimeError):
        store.create_evidence(evidence_id="evidence_bad", topic_id="topic_test", locator=inconsistent,
                              source_revision_sha256="b"*64, acquisition_method=EvidenceAcquisitionMethod.USER_SELECTED,
                              retrieval_status="not_requested", limitation_codes=())
    assert store.get_bundle_rows("topic_test") == before
