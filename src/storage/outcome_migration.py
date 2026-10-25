"""Transaction-owned 014 backfill and readonly Outcome schema verification.

No path, configuration, Provider, or Markdown access belongs here. The manager
supplies an already isolated work connection; the canonical input hash is the
same function used by normal Outcome mutations.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from src.outcomes.identity import (
    RENDER_CONTRACT_VERSION,
    outcome_input_digest,
    outcome_publication_id,
    outcome_review_receipt_id,
)
from src.outcomes.models import EvidenceLocator
from src.runtime.errors import ErrorCode, PKVRuntimeError


MIGRATION_VERSION = "1.2.8"
MIGRATION_NAME = "014_add_lifecycle_consistency.sql"
_TOPIC_FIELDS = ("topic_id", "outcome_id", "title", "question", "state", "revision")
_EVIDENCE_FIELDS = (
    "evidence_id", "ordinal", "locator", "locator_kind", "source_revision_sha256",
    "acquisition_method", "retrieval_status", "recorded_state", "limitation_codes",
)
_CLAIM_FIELDS = ("claim_id", "ordinal", "statement", "state", "revision", "limitation_codes")
_OLD_TABLES = (
    "outcome_topics", "outcome_evidence", "outcome_claims", "outcome_claim_evidence", "outcome_briefs",
)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, args)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _validate_evidence_locator(evidence: Mapping[str, Any]) -> None:
    locator = EvidenceLocator.parse(evidence["locator"])
    if (
        evidence["locator_kind"] != locator.kind.value
        or evidence["knowledge_id"] != locator.knowledge_id
        or evidence["chunk_id"] != locator.chunk_id
        or evidence["relation_id"] != locator.relation_id
    ):
        raise ValueError("Outcome evidence locator and auxiliary identity disagree")


def input_digest_from_connection(conn: sqlite3.Connection, topic: Mapping[str, Any]) -> str:
    """Bind exactly the persisted input without opening external evidence."""

    evidence = _rows(conn, "SELECT * FROM outcome_evidence WHERE topic_id=?", (topic["topic_id"],))
    claims = _rows(conn, "SELECT * FROM outcome_claims WHERE topic_id=?", (topic["topic_id"],))
    projected_evidence = []
    for item in evidence:
        _validate_evidence_locator(item)
        projected = {name: item[name] for name in _EVIDENCE_FIELDS}
        projected["limitation_codes"] = json.loads(projected["limitation_codes"])
        projected_evidence.append(projected)
    projected_claims = []
    for item in claims:
        projected = {name: item[name] for name in _CLAIM_FIELDS}
        projected["limitation_codes"] = json.loads(projected["limitation_codes"])
        projected["evidence_links"] = _rows(
            conn,
            "SELECT evidence_id, polarity FROM outcome_claim_evidence WHERE claim_id=?",
            (item["claim_id"],),
        )
        projected_claims.append(projected)
    return outcome_input_digest(
        input_revision=topic["input_revision"],
        render_contract_version=topic["render_contract_version"],
        topic={name: topic[name] for name in _TOPIC_FIELDS},
        evidence=projected_evidence,
        claims=projected_claims,
    )


def _schema_body(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if row is None or not isinstance(row[0], str) or "(" not in row[0]:
        raise ValueError("Outcome table is absent")
    # ALTER TABLE changes the table-name quoting, not the declaration body.
    # Preserve quoted literals exactly: removing whitespace globally would make
    # a changed CHECK value such as 'ap prove' look identical to 'approve'.
    return row[0][row[0].index("("):].strip()


def _verify_indexes(conn: sqlite3.Connection, expected: sqlite3.Connection, tables: tuple[str, ...]) -> None:
    def signatures(connection: sqlite3.Connection, table: str) -> set[tuple]:
        result = set()
        for row in connection.execute(f'PRAGMA index_list("{table}")'):
            name = str(row[1])
            columns = tuple(item[2] for item in connection.execute(f'PRAGMA index_info("{name}")'))
            result.add((int(row[2]), row[3], int(row[4]), columns))
        return result
    for table in tables:
        if signatures(conn, table) != signatures(expected, table):
            raise ValueError("Outcome indexes do not match the schema")


def _expected_schema(legacy_sql: str, target_sql: str | None) -> sqlite3.Connection:
    expected = sqlite3.connect(":memory:")
    try:
        expected.executescript("CREATE TABLE schema_version(version TEXT UNIQUE, description TEXT);\n" + legacy_sql)
        if target_sql is not None:
            expected.executescript(target_sql)
            expected.execute("DROP TABLE outcome_briefs")
            expected.execute("DROP TABLE outcome_topics")
            _rename_tables(expected)
        return expected
    except BaseException:
        expected.close()
        raise


def _rename_tables(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE _outcome_topics_v2 RENAME TO outcome_topics")
    conn.execute("ALTER TABLE _outcome_briefs_v2 RENAME TO outcome_briefs")
    conn.execute("CREATE INDEX idx_outcome_briefs_state ON outcome_briefs(state, topic_id)")


def validate_outcome_schema(
    conn: sqlite3.Connection, *, legacy_sql: str, target_sql: str | None = None,
    validate_bindings: bool = True,
) -> None:
    """Reject table-name-only or forged-version databases, without any writes."""

    expected: sqlite3.Connection | None = None
    owns_read_transaction = not conn.in_transaction
    tables = _OLD_TABLES + (("outcome_review_receipts",) if target_sql is not None else ())
    try:
        if owns_read_transaction:
            conn.execute("BEGIN")
        expected = _expected_schema(legacy_sql, target_sql)
        for table in tables:
            if _schema_body(conn, table) != _schema_body(expected, table):
                raise ValueError("Outcome declarations do not match the version")
        _verify_indexes(conn, expected, tables)
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Outcome foreign keys are invalid")
        crossing = conn.execute(
            "SELECT 1 FROM outcome_claim_evidence l "
            "JOIN outcome_claims c ON c.claim_id=l.claim_id "
            "JOIN outcome_evidence e ON e.evidence_id=l.evidence_id "
            "WHERE c.topic_id<>e.topic_id LIMIT 1"
        ).fetchone()
        if crossing:
            raise ValueError("Outcome evidence belongs to another topic")
        for evidence in _rows(conn, "SELECT * FROM outcome_evidence"):
            _validate_evidence_locator(evidence)
        if target_sql is not None and validate_bindings:
            _validate_bindings(conn)
    except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        raise PKVRuntimeError(
            ErrorCode.DATABASE_SCHEMA_DRIFT,
            "Outcome schema 或输入绑定不符合已声明版本。",
            stage="outcome_migration",
        ) from exc
    finally:
        if expected is not None:
            expected.close()
        if owns_read_transaction:
            conn.rollback()


def _validate_bindings(conn: sqlite3.Connection) -> None:
    for topic in _rows(conn, "SELECT * FROM outcome_topics"):
        if topic["render_contract_version"] != RENDER_CONTRACT_VERSION:
            raise ValueError("Unsupported Outcome renderer contract")
        if topic["input_digest"] != input_digest_from_connection(conn, topic):
            raise ValueError("Outcome input digest mismatch")
    for brief in _rows(conn, "SELECT * FROM outcome_briefs"):
        if brief["binding_state"] == "verified":
            if brief["render_contract_version"] != RENDER_CONTRACT_VERSION:
                raise ValueError("Unsupported published Outcome renderer")
            if brief["publication_id"] != outcome_publication_id(
                outcome_id=brief["outcome_id"], candidate_revision=brief["revision"],
                input_digest=brief["rendered_input_digest"],
            ):
                raise ValueError("Outcome publication identity mismatch")
    for receipt in _rows(conn, "SELECT * FROM outcome_review_receipts"):
        if receipt["receipt_id"] != outcome_review_receipt_id(**{
            name: receipt[name] for name in (
                "outcome_id", "expected_revision", "content_sha256", "expected_input_revision",
                "action", "reason_code",
            )
        }):
            raise ValueError("Outcome review receipt identity mismatch")


def backfill_outcome_bindings(conn: sqlite3.Connection) -> None:
    """Copy legacy rows and bind only current input; caller owns the transaction."""

    if not conn.in_transaction or conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise ValueError("014 requires a transaction-owned private work connection")
    topics = _rows(conn, "SELECT * FROM outcome_topics ORDER BY topic_id")
    briefs = _rows(conn, "SELECT * FROM outcome_briefs ORDER BY topic_id")
    identities = {brief["topic_id"]: brief["outcome_id"] for brief in briefs}
    for original in topics:
        topic = dict(original)
        topic.update(
            outcome_id=identities.get(topic["topic_id"]) or f"outcome-{uuid.uuid4().hex}",
            input_revision=1,
            render_contract_version=RENDER_CONTRACT_VERSION,
        )
        topic["input_digest"] = input_digest_from_connection(conn, topic)
        columns = tuple(topic)
        conn.execute(
            "INSERT INTO _outcome_topics_v2 (" + ",".join(columns) + ") VALUES ("
            + ",".join("?" for _ in columns) + ")", tuple(topic[column] for column in columns),
        )
    for original in briefs:
        brief = dict(original)
        brief.update(
            binding_state="legacy_unverified", publication_id=None,
            rendered_input_revision=None, rendered_input_digest=None, render_contract_version=None,
        )
        columns = tuple(brief)
        conn.execute(
            "INSERT INTO _outcome_briefs_v2 (" + ",".join(columns) + ") VALUES ("
            + ",".join("?" for _ in columns) + ")", tuple(brief[column] for column in columns),
        )
    conn.execute("DROP TABLE outcome_briefs")
    conn.execute("DROP TABLE outcome_topics")
    _rename_tables(conn)


__all__ = ["backfill_outcome_bindings", "input_digest_from_connection", "validate_outcome_schema"]
