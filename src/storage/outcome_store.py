"""SQLite authority for P1-B traceable knowledge outcomes.

This store is intentionally narrow: it never creates or upgrades a schema,
never writes a Markdown file, and never resolves a source locator.  The
Application-owned outcome service holds the R3 writer lease, captures source
revisions, publishes a revisioned Markdown projection, then commits the
pointer here.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from src.outcomes.models import (
    ClaimEvidenceLink,
    ClaimEvidencePolarity,
    ClaimRecord,
    ClaimState,
    EvidenceAcquisitionMethod,
    EvidenceLocator,
    EvidenceRecord,
    EvidenceState,
    OutcomeState,
    ResearchBriefRecord,
    ReviewCommitResult,
    TopicRecord,
    TopicState,
    normalize_limitation_codes,
    normalize_outcome_text,
)
from src.outcomes.identity import (
    RENDER_CONTRACT_VERSION, outcome_input_digest,
    outcome_publication_id, outcome_review_receipt_id, require_revision,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer
from src.storage.sqlite_connection import connect_existing_sqlite

_REQUIRED_TABLES = frozenset(
    {
        "outcome_topics",
        "outcome_evidence",
        "outcome_claims",
        "outcome_claim_evidence",
        "outcome_briefs",
        "outcome_review_receipts",
    }
)


class OutcomeStore:
    """Low-level structured-state store for Topic/Evidence/Claim/Brief rows."""

    def __init__(self, db_path: Path, *, layout: object) -> None:
        self.db_path = Path(db_path)
        self.layout = layout

    @contextmanager
    def get_connection(self) -> Iterator[sqlite3.Connection]:
        """Open an existing SQLite database and commit one scoped transaction."""

        conn = connect_existing_sqlite(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            # SELECTs must share a snapshot too; sqlite's implicit transaction
            # starts only at DML. Product writes additionally hold the root lease.
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _require_schema(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        present = {str(row[0]) for row in rows}
        versions = ({str(row[0]) for row in conn.execute("SELECT version FROM schema_version")}
                    if "schema_version" in present else set())
        if "1.2.8" not in versions:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_UPGRADE_REQUIRED,
                "知识库需要升级后才能使用可追溯成果。",
                stage="outcome_schema",
                recoverable=True,
            )
        required_columns = {
            "outcome_topics": {"topic_id", "outcome_id", "input_revision", "input_digest", "render_contract_version"},
            "outcome_briefs": {"binding_state", "publication_id", "rendered_input_revision", "rendered_input_digest", "render_contract_version"},
            "outcome_review_receipts": {"receipt_id", "outcome_id", "expected_revision", "content_sha256", "expected_input_revision", "action", "reason_code", "result_state", "created_at"},
        }
        if not _REQUIRED_TABLES.issubset(present) or any(
            not columns.issubset({str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')})
            for table, columns in required_columns.items()
        ):
            raise PKVRuntimeError(ErrorCode.DATABASE_SCHEMA_DRIFT, "成果数据库结构不符合已声明版本。", stage="outcome_schema")
        # Application callers can construct the Store without bootstrap. Reuse
        # the migration contract on this same read snapshot instead of trusting
        # only a claimed version and table/column names.
        from src.storage.migration_manager import _read_migration_text
        from src.storage.outcome_migration import validate_outcome_schema

        migrations_dir = Path(self.layout.migrations_dir)
        validate_outcome_schema(
            conn,
            legacy_sql=_read_migration_text(migrations_dir / "013_add_traceable_outcomes.sql"),
            target_sql=_read_migration_text(migrations_dir / "014_add_lifecycle_consistency.sql"),
            validate_bindings=False,
        )

    def _require_write(self) -> None:
        require_active_data_root_writer(self.layout, owner="knowledge_outcomes")

    def _require_input_binding(self, conn: sqlite3.Connection, topic_id: str) -> None:
        bundle = self._bundle_from_connection(conn, topic_id)
        if bundle is not None and bundle[0].render_contract_version != RENDER_CONTRACT_VERSION:
            raise PKVRuntimeError(ErrorCode.OUTCOME_RENDER_CONTRACT_UNSUPPORTED, "成果渲染合同版本不受支持。", stage="outcome_input")

    @staticmethod
    def _topic_from_row(row: sqlite3.Row) -> TopicRecord:
        return TopicRecord(
            topic_id=str(row["topic_id"]),
            title=str(row["title"]),
            question=str(row["question"]),
            state=TopicState(str(row["state"])),
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            outcome_id=str(row["outcome_id"]),
            input_revision=int(row["input_revision"]),
            input_digest=str(row["input_digest"]),
            render_contract_version=str(row["render_contract_version"]),
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
        locator = EvidenceLocator.parse(str(row["locator"]))
        if (row["locator_kind"], row["knowledge_id"], row["chunk_id"], row["relation_id"]) != (
            locator.kind.value, locator.knowledge_id, locator.chunk_id, locator.relation_id
        ):
            raise PKVRuntimeError(ErrorCode.DATABASE_SCHEMA_DRIFT, "成果证据定位字段不一致。", stage="outcome_binding")
        return EvidenceRecord(
            evidence_id=str(row["evidence_id"]),
            topic_id=str(row["topic_id"]),
            ordinal=row["ordinal"],
            locator=locator,
            source_revision_sha256=str(row["source_revision_sha256"]),
            acquisition_method=EvidenceAcquisitionMethod(str(row["acquisition_method"])),
            retrieval_status=str(row["retrieval_status"]),
            limitation_codes=normalize_limitation_codes(
                json.loads(str(row["limitation_codes"]))
            ),
            recorded_state=EvidenceState(str(row["recorded_state"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _claim_from_row(
        row: sqlite3.Row,
        links: tuple[ClaimEvidenceLink, ...],
    ) -> ClaimRecord:
        return ClaimRecord(
            claim_id=str(row["claim_id"]),
            topic_id=str(row["topic_id"]),
            ordinal=row["ordinal"],
            statement=str(row["statement"]),
            state=ClaimState(str(row["state"])),
            limitation_codes=normalize_limitation_codes(
                json.loads(str(row["limitation_codes"]))
            ),
            revision=row["revision"],
            evidence_links=links,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _brief_from_row(row: sqlite3.Row) -> ResearchBriefRecord:
        return ResearchBriefRecord(
            outcome_id=str(row["outcome_id"]),
            topic_id=str(row["topic_id"]),
            state=OutcomeState(str(row["state"])),
            revision=int(row["revision"]),
            markdown_relative_path=str(row["markdown_relative_path"]),
            content_sha256=str(row["content_sha256"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            binding_state=str(row["binding_state"]),
            publication_id=row["publication_id"],
            rendered_input_revision=row["rendered_input_revision"],
            rendered_input_digest=row["rendered_input_digest"],
            render_contract_version=row["render_contract_version"],
        )

    @staticmethod
    def _topic_or_raise(conn: sqlite3.Connection, topic_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM outcome_topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_NOT_FOUND,
                "指定的研究主题不存在。",
                stage="outcome_topic",
                recoverable=False,
            )
        return row

    @staticmethod
    def _claim_or_raise(conn: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM outcome_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_NOT_FOUND,
                "指定的主张不存在。",
                stage="outcome_claim",
                recoverable=False,
            )
        return row

    @staticmethod
    def _evidence_or_raise(conn: sqlite3.Connection, evidence_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM outcome_evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.OUTCOME_NOT_FOUND,
                "指定的证据不存在。",
                stage="outcome_evidence",
                recoverable=False,
            )
        return row

    def _touch_topic(self, conn: sqlite3.Connection, topic_id: str) -> None:
        """Advance the complete input and invalidate its publication atomically."""
        topic = self._topic_or_raise(conn, topic_id)
        require_revision(int(topic["input_revision"]) + 1)
        conn.execute(
            """
            UPDATE outcome_topics
            SET input_revision = input_revision + 1, updated_at = CURRENT_TIMESTAMP
            WHERE topic_id = ?
            """,
            (topic_id,),
        )
        from src.storage.outcome_migration import input_digest_from_connection

        self._bundle_from_connection(conn, topic_id, verify=False)
        digest = input_digest_from_connection(conn, dict(self._topic_or_raise(conn, topic_id)))
        conn.execute("UPDATE outcome_topics SET input_digest=? WHERE topic_id=?", (digest, topic_id))
        conn.execute(
            "UPDATE outcome_briefs SET state=?, updated_at=CURRENT_TIMESTAMP WHERE topic_id=? AND state!=?",
            (OutcomeState.NEEDS_REVIEW.value, topic_id, OutcomeState.NEEDS_REVIEW.value),
        )

    @staticmethod
    def _claim_links(
        conn: sqlite3.Connection,
        claim_id: str,
    ) -> tuple[ClaimEvidenceLink, ...]:
        rows = conn.execute(
            """
            SELECT cle.evidence_id, cle.polarity
            FROM outcome_claim_evidence cle
            JOIN outcome_evidence oe ON oe.evidence_id = cle.evidence_id
            WHERE cle.claim_id = ?
            ORDER BY oe.ordinal ASC, cle.evidence_id ASC
            """,
            (claim_id,),
        ).fetchall()
        return tuple(
            ClaimEvidenceLink(
                evidence_id=str(row["evidence_id"]),
                polarity=ClaimEvidencePolarity(str(row["polarity"])),
            )
            for row in rows
        )

    def create_topic(
        self,
        *,
        topic_id: str,
        title: str,
        question: str,
    ) -> TopicRecord:
        self._require_write()
        clean_title = normalize_outcome_text(title, label="topic title", maximum=240)
        clean_question = normalize_outcome_text(
            question,
            label="topic question",
            maximum=4_000,
        )
        with self.get_connection() as conn:
            self._require_schema(conn)
            outcome_id = f"outcome-{uuid.uuid4().hex}"
            digest = outcome_input_digest(
                topic={"topic_id": topic_id, "outcome_id": outcome_id, "title": clean_title,
                       "question": clean_question, "state": "active", "revision": 1},
                evidence=(), claims=(), input_revision=1, render_contract_version=RENDER_CONTRACT_VERSION,
            )
            conn.execute(
                """
                INSERT INTO outcome_topics (topic_id, title, question, state, outcome_id,
                    input_revision, input_digest, render_contract_version)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (topic_id, clean_title, clean_question, TopicState.ACTIVE.value,
                 outcome_id, digest, RENDER_CONTRACT_VERSION),
            )
            row = self._topic_or_raise(conn, topic_id)
            return self._topic_from_row(row)

    def get_topic(self, topic_id: str) -> TopicRecord | None:
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            return self._topic_from_row(row) if row is not None else None

    def update_topic(
        self, *, topic_id: str, title: str | None = None, question: str | None = None,
        state: TopicState | None = None, expected_revision: int | None = None,
    ) -> TopicRecord:
        """Edit owned Topic fields without conflating their revision with input."""
        self._require_write()
        if title is None and question is None and state is None:
            raise ValueError("至少需要提供一个topic更新字段")
        if expected_revision is not None:
            require_revision(expected_revision)
        if state is not None and type(state) is not TopicState:
            raise TypeError("topic state无效")
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = self._topic_or_raise(conn, topic_id)
            if expected_revision is not None and expected_revision != row["revision"]:
                self._conflict()
            self._require_input_binding(conn, topic_id)
            new_title = normalize_outcome_text(title, label="topic title", maximum=240) if title is not None else row["title"]
            new_question = normalize_outcome_text(question, label="topic question", maximum=4000) if question is not None else row["question"]
            new_state = state.value if state is not None else row["state"]
            if (new_title, new_question, new_state) == (row["title"], row["question"], row["state"]):
                return self._topic_from_row(row)
            require_revision(row["revision"] + 1)
            conn.execute("UPDATE outcome_topics SET title=?,question=?,state=?,revision=revision+1 WHERE topic_id=?",
                         (new_title, new_question, new_state, topic_id))
            self._touch_topic(conn, topic_id)
            return self._topic_from_row(self._topic_or_raise(conn, topic_id))

    def list_topics(self, *, limit: int = 100, offset: int = 0) -> tuple[TopicRecord, ...]:
        if type(limit) is not int or limit < 1 or limit > 500:
            raise ValueError("topic limit必须在1到500之间")
        if type(offset) is not int or offset < 0:
            raise ValueError("topic offset必须是非负整数")
        with self.get_connection() as conn:
            self._require_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM outcome_topics
                ORDER BY created_at ASC, topic_id ASC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return tuple(self._topic_from_row(row) for row in rows)

    def create_evidence(
        self,
        *,
        evidence_id: str,
        topic_id: str,
        locator: EvidenceLocator,
        source_revision_sha256: str,
        acquisition_method: EvidenceAcquisitionMethod,
        retrieval_status: str,
        limitation_codes: tuple[str, ...],
    ) -> EvidenceRecord:
        self._require_write()
        if retrieval_status not in {"not_requested", "success", "degraded"}:
            raise ValueError("evidence retrieval_status无效")
        limitations = normalize_limitation_codes(limitation_codes)
        with self.get_connection() as conn:
            self._require_schema(conn)
            topic = self._topic_or_raise(conn, topic_id)
            self._require_input_binding(conn, topic_id)
            if str(topic["state"]) != TopicState.ACTIVE.value:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_STATE_CONFLICT,
                    "已关闭的研究主题不能添加证据。",
                    stage="outcome_topic",
                    recoverable=False,
                )
            ordinal = int(topic["next_evidence_ordinal"])
            conn.execute(
                """
                INSERT INTO outcome_evidence (
                    evidence_id, topic_id, ordinal, locator, locator_kind,
                    knowledge_id, chunk_id, relation_id, source_revision_sha256,
                    acquisition_method, retrieval_status, limitation_codes, recorded_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    topic_id,
                    ordinal,
                    locator.value,
                    locator.kind.value,
                    locator.knowledge_id,
                    locator.chunk_id,
                    locator.relation_id,
                    source_revision_sha256,
                    acquisition_method.value,
                    retrieval_status,
                    json.dumps(list(limitations), ensure_ascii=True, separators=(",", ":")),
                    EvidenceState.CURRENT.value,
                ),
            )
            conn.execute(
                """
                UPDATE outcome_topics
                SET next_evidence_ordinal = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE topic_id = ?
                """,
                (ordinal + 1, topic_id),
            )
            self._touch_topic(conn, topic_id)
            row = self._evidence_or_raise(conn, evidence_id)
            return self._evidence_from_row(row)

    def delete_evidence(self, evidence_id: str) -> str:
        """Detach one selected evidence item and return its owning topic id."""

        self._require_write()
        with self.get_connection() as conn:
            self._require_schema(conn)
            evidence = self._evidence_or_raise(conn, evidence_id)
            topic_id = str(evidence["topic_id"])
            self._require_input_binding(conn, topic_id)
            conn.execute("DELETE FROM outcome_evidence WHERE evidence_id = ?", (evidence_id,))
            self._touch_topic(conn, topic_id)
            return topic_id

    def update_evidence_recorded_states(
        self,
        updates: dict[str, EvidenceState],
    ) -> tuple[str, ...]:
        """Persist only observed binding-state changes and return touched topics."""

        if not updates:
            return ()
        self._require_write()
        touched: set[str] = set()
        with self.get_connection() as conn:
            self._require_schema(conn)
            input_rows = {evidence_id: self._evidence_or_raise(conn, evidence_id) for evidence_id in updates}
            for topic_id in {str(row["topic_id"]) for row in input_rows.values()}:
                self._require_input_binding(conn, topic_id)
            for evidence_id, state in updates.items():
                if type(state) is not EvidenceState:
                    raise TypeError("evidence state无效")
                row = input_rows[evidence_id]
                topic_id = str(row["topic_id"])
                if str(row["recorded_state"]) == state.value:
                    continue
                conn.execute(
                    """
                    UPDATE outcome_evidence
                    SET recorded_state = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE evidence_id = ?
                    """,
                    (state.value, evidence_id),
                )
                touched.add(topic_id)
            for topic_id in sorted(touched):
                self._touch_topic(conn, topic_id)
        return tuple(sorted(touched))

    def create_claim(
        self,
        *,
        claim_id: str,
        topic_id: str,
        statement: str,
        state: ClaimState,
        limitation_codes: tuple[str, ...],
    ) -> ClaimRecord:
        self._require_write()
        clean_statement = normalize_outcome_text(
            statement,
            label="claim statement",
            maximum=8_000,
        )
        limitations = normalize_limitation_codes(limitation_codes)
        with self.get_connection() as conn:
            self._require_schema(conn)
            topic = self._topic_or_raise(conn, topic_id)
            self._require_input_binding(conn, topic_id)
            if str(topic["state"]) != TopicState.ACTIVE.value:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_STATE_CONFLICT,
                    "已关闭的研究主题不能添加主张。",
                    stage="outcome_topic",
                    recoverable=False,
                )
            ordinal = int(topic["next_claim_ordinal"])
            conn.execute(
                """
                INSERT INTO outcome_claims (
                    claim_id, topic_id, ordinal, statement, state, limitation_codes
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    topic_id,
                    ordinal,
                    clean_statement,
                    state.value,
                    json.dumps(list(limitations), ensure_ascii=True, separators=(",", ":")),
                ),
            )
            conn.execute(
                """
                UPDATE outcome_topics
                SET next_claim_ordinal = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE topic_id = ?
                """,
                (ordinal + 1, topic_id),
            )
            self._touch_topic(conn, topic_id)
            row = self._claim_or_raise(conn, claim_id)
            return self._claim_from_row(row, self._claim_links(conn, claim_id))

    def update_claim(
        self,
        *,
        claim_id: str,
        statement: str | None,
        state: ClaimState | None,
        limitation_codes: tuple[str, ...] | None,
        expected_revision: int | None,
    ) -> ClaimRecord:
        self._require_write()
        if statement is None and state is None and limitation_codes is None:
            raise ValueError("至少需要提供一个claim更新字段")
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1
        ):
            raise ValueError("expected_revision无效")
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = self._claim_or_raise(conn, claim_id)
            self._require_input_binding(conn, str(row["topic_id"]))
            existing_revision = int(row["revision"])
            if expected_revision is not None and expected_revision != existing_revision:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_STATE_CONFLICT,
                    "主张已被其他操作更新，请重新读取后再修改。",
                    stage="outcome_claim",
                    recoverable=True,
                )
            clean_statement = (
                normalize_outcome_text(
                    statement,
                    label="claim statement",
                    maximum=8_000,
                )
                if statement is not None
                else str(row["statement"])
            )
            next_state = state or ClaimState(str(row["state"]))
            next_limits = (
                normalize_limitation_codes(limitation_codes)
                if limitation_codes is not None
                else normalize_limitation_codes(json.loads(str(row["limitation_codes"])))
            )
            if (clean_statement == str(row["statement"]) and next_state.value == str(row["state"])
                    and next_limits == normalize_limitation_codes(json.loads(str(row["limitation_codes"])))):
                return self._claim_from_row(row, self._claim_links(conn, claim_id))
            require_revision(existing_revision + 1)
            cursor = conn.execute(
                """
                UPDATE outcome_claims
                SET statement = ?, state = ?, limitation_codes = ?, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE claim_id = ? AND revision = ?
                """,
                (
                    clean_statement,
                    next_state.value,
                    json.dumps(list(next_limits), ensure_ascii=True, separators=(",", ":")),
                    claim_id,
                    existing_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_STATE_CONFLICT,
                    "主张更新冲突，请重新读取后再修改。",
                    stage="outcome_claim",
                    recoverable=True,
                )
            topic_id = str(row["topic_id"])
            self._touch_topic(conn, topic_id)
            updated = self._claim_or_raise(conn, claim_id)
            return self._claim_from_row(updated, self._claim_links(conn, claim_id))

    def link_claim_evidence(
        self,
        *,
        claim_id: str,
        evidence_id: str,
        polarity: ClaimEvidencePolarity,
    ) -> ClaimRecord:
        self._require_write()
        with self.get_connection() as conn:
            self._require_schema(conn)
            claim = self._claim_or_raise(conn, claim_id)
            self._require_input_binding(conn, str(claim["topic_id"]))
            evidence = self._evidence_or_raise(conn, evidence_id)
            if str(claim["topic_id"]) != str(evidence["topic_id"]):
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_INVALID,
                    "主张和证据必须属于同一研究主题。",
                    stage="outcome_claim_evidence",
                    recoverable=False,
                )
            existing = conn.execute(
                "SELECT polarity FROM outcome_claim_evidence WHERE claim_id=? AND evidence_id=?",
                (claim_id, evidence_id),
            ).fetchone()
            if existing is not None and str(existing["polarity"]) == polarity.value:
                return self._claim_from_row(claim, self._claim_links(conn, claim_id))
            conn.execute(
                """
                INSERT INTO outcome_claim_evidence (claim_id, evidence_id, polarity)
                VALUES (?, ?, ?)
                ON CONFLICT(claim_id, evidence_id) DO UPDATE SET polarity = excluded.polarity
                """,
                (claim_id, evidence_id, polarity.value),
            )
            self._touch_topic(conn, str(claim["topic_id"]))
            updated = self._claim_or_raise(conn, claim_id)
            return self._claim_from_row(updated, self._claim_links(conn, claim_id))

    def unlink_claim_evidence(self, *, claim_id: str, evidence_id: str) -> ClaimRecord:
        self._require_write()
        with self.get_connection() as conn:
            self._require_schema(conn)
            claim = self._claim_or_raise(conn, claim_id)
            self._require_input_binding(conn, str(claim["topic_id"]))
            cursor = conn.execute(
                """
                DELETE FROM outcome_claim_evidence
                WHERE claim_id = ? AND evidence_id = ?
                """,
                (claim_id, evidence_id),
            )
            if cursor.rowcount != 1:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_NOT_FOUND,
                    "指定的主张证据绑定不存在。",
                    stage="outcome_claim_evidence",
                    recoverable=False,
                )
            self._touch_topic(conn, str(claim["topic_id"]))
            updated = self._claim_or_raise(conn, claim_id)
            return self._claim_from_row(updated, self._claim_links(conn, claim_id))

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_claims WHERE claim_id = ?", (claim_id,)
            ).fetchone()
            if row is None:
                return None
            return self._claim_from_row(row, self._claim_links(conn, claim_id))

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            return self._evidence_from_row(row) if row is not None else None

    def get_brief_by_topic(self, topic_id: str) -> ResearchBriefRecord | None:
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            return self._bundle_from_connection(conn, topic_id)[3] if row is not None else None

    def get_brief(self, outcome_id: str) -> ResearchBriefRecord | None:
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE outcome_id = ?", (outcome_id,)
            ).fetchone()
            return self._bundle_from_connection(conn, str(row["topic_id"]))[3] if row is not None else None

    def mark_brief_needs_review(self, topic_id: str) -> ResearchBriefRecord | None:
        """Invalidate a previously rendered brief after explicit source-state edits."""

        self._require_write()
        with self.get_connection() as conn:
            self._require_schema(conn)
            self._topic_or_raise(conn, topic_id)
            conn.execute(
                """
                UPDATE outcome_briefs
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE topic_id = ? AND state != ?
                """,
                (OutcomeState.NEEDS_REVIEW.value, topic_id, OutcomeState.NEEDS_REVIEW.value),
            )
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            return self._brief_from_row(row) if row is not None else None

    def upsert_rendered_brief(
        self,
        *,
        outcome_id: str,
        topic_id: str,
        revision: int,
        markdown_relative_path: str,
        content_sha256: str,
        expected_previous_revision: int | None,
        expected_previous_pointer: ResearchBriefRecord | None,
        expected_input_revision: int,
        expected_input_digest: str,
        render_contract_version: str,
        publication_id: str,
    ) -> ResearchBriefRecord:
        """Commit a pointer to an already atomically published immutable Markdown file."""

        self._require_write()
        require_revision(revision)
        require_revision(expected_input_revision)
        if expected_previous_revision is not None:
            require_revision(expected_previous_revision)
        if render_contract_version != RENDER_CONTRACT_VERSION:
            raise PKVRuntimeError(ErrorCode.OUTCOME_RENDER_CONTRACT_UNSUPPORTED, "成果渲染合同版本不受支持。", stage="outcome_brief")
        if publication_id != outcome_publication_id(outcome_id=outcome_id, candidate_revision=revision, input_digest=expected_input_digest):
            raise ValueError("outcome publication_id不匹配")
        # Validate path/hash before persistence, not after a partially issued write.
        ResearchBriefRecord(outcome_id=outcome_id, topic_id=topic_id, state=OutcomeState.AWAITING_REVIEW,
                            revision=revision, markdown_relative_path=markdown_relative_path,
                            content_sha256=content_sha256, created_at="", updated_at="")
        with self.get_connection() as conn:
            self._require_schema(conn)
            self._topic_or_raise(conn, topic_id)
            topic, _, _, _ = self._bundle_from_connection(conn, topic_id)
            if (topic.outcome_id, topic.input_revision, topic.input_digest, topic.render_contract_version) != (
                outcome_id, expected_input_revision, expected_input_digest, render_contract_version
            ):
                self._conflict()
            existing = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if existing is None:
                if expected_previous_revision is not None or expected_previous_pointer is not None or revision != 1:
                    raise PKVRuntimeError(
                        ErrorCode.OUTCOME_STATE_CONFLICT,
                        "研究成果版本已变化，请重新生成。",
                        stage="outcome_brief",
                        recoverable=True,
                    )
                conn.execute(
                    """
                    INSERT INTO outcome_briefs (
                        outcome_id, topic_id, outcome_type, state, revision,
                        markdown_relative_path, content_sha256, binding_state,
                        publication_id, rendered_input_revision, rendered_input_digest, render_contract_version
                    ) VALUES (?, ?, 'research_brief', ?, ?, ?, ?, 'verified', ?, ?, ?, ?)
                    """,
                    (
                        outcome_id,
                        topic_id,
                        OutcomeState.AWAITING_REVIEW.value,
                        revision,
                        markdown_relative_path,
                        content_sha256,
                        publication_id, expected_input_revision, expected_input_digest, render_contract_version,
                    ),
                )
            else:
                previous_revision = int(existing["revision"])
                if (
                    expected_previous_revision != previous_revision
                    or revision != previous_revision + 1
                    or str(existing["outcome_id"]) != outcome_id
                    or expected_previous_pointer is None
                    or self._pointer_tuple(self._brief_from_row(existing)) != self._pointer_tuple(expected_previous_pointer)
                ):
                    raise PKVRuntimeError(
                        ErrorCode.OUTCOME_STATE_CONFLICT,
                        "研究成果版本已变化，请重新生成。",
                        stage="outcome_brief",
                        recoverable=True,
                    )
                conn.execute(
                    """
                    UPDATE outcome_briefs
                    SET state = ?, revision = ?, markdown_relative_path = ?,
                        content_sha256 = ?, binding_state = 'verified', publication_id = ?,
                        rendered_input_revision = ?, rendered_input_digest = ?, render_contract_version = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE topic_id = ?
                    """,
                    (
                        OutcomeState.AWAITING_REVIEW.value,
                        revision,
                        markdown_relative_path,
                        content_sha256,
                        publication_id, expected_input_revision, expected_input_digest, render_contract_version,
                        topic_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("outcome brief upsert后未找到记录")
            return self._with_current(self._brief_from_row(row), topic)

    @staticmethod
    def _pointer_tuple(brief: ResearchBriefRecord) -> tuple:
        return (brief.outcome_id, brief.revision, brief.markdown_relative_path,
                brief.content_sha256, brief.publication_id)

    @staticmethod
    def _conflict() -> None:
        raise PKVRuntimeError(ErrorCode.OUTCOME_STATE_CONFLICT, "成果或输入版本已变化，请重新读取后确认。", stage="outcome_review", recoverable=True)

    @staticmethod
    def _with_current(brief: ResearchBriefRecord, topic: TopicRecord) -> ResearchBriefRecord:
        changed = (brief.binding_state != "verified" or
                   (brief.rendered_input_revision, brief.rendered_input_digest, brief.render_contract_version) !=
                   (topic.input_revision, topic.input_digest, topic.render_contract_version))
        return replace(brief, current_input_revision=topic.input_revision,
                       current_input_digest=topic.input_digest,
                       current_render_contract_version=topic.render_contract_version,
                       effective_state=OutcomeState.NEEDS_REVIEW if changed else brief.effective_state)

    def review_brief(
        self, *, outcome_id: str, expected_revision: int, content_sha256: str,
        expected_input_revision: int, action: str, reason_code: str | None,
    ) -> ReviewCommitResult:
        """CAS the exact publication and append its review receipt in one commit."""
        self._require_write()
        receipt_id = outcome_review_receipt_id(
            outcome_id=outcome_id, expected_revision=expected_revision, content_sha256=content_sha256,
            expected_input_revision=expected_input_revision, action=action, reason_code=reason_code,
        )
        result_state = OutcomeState.APPROVED if action == "approve" else OutcomeState.RETURNED
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute("SELECT * FROM outcome_briefs WHERE outcome_id=?", (outcome_id,)).fetchone()
            if row is None:
                raise PKVRuntimeError(ErrorCode.OUTCOME_NOT_FOUND, "研究成果不存在。", stage="outcome_review")
            topic, _, _, brief = self._bundle_from_connection(conn, str(row["topic_id"]))
            assert brief is not None
            if (brief.revision, brief.content_sha256, topic.input_revision) != (
                expected_revision, content_sha256, expected_input_revision
            ):
                self._conflict()
            if brief.binding_state != "verified":
                raise PKVRuntimeError(ErrorCode.OUTCOME_APPROVAL_BLOCKED, "成果需要重新生成后才能确认。", stage="outcome_review")
            if (brief.rendered_input_revision, brief.rendered_input_digest, brief.render_contract_version) != (
                topic.input_revision, topic.input_digest, topic.render_contract_version
            ):
                self._conflict()
            if brief.visible_state in {OutcomeState.DRAFT, OutcomeState.NEEDS_REVIEW}:
                raise PKVRuntimeError(ErrorCode.OUTCOME_APPROVAL_BLOCKED, "成果需要重新生成后才能确认。", stage="outcome_review")
            existing = conn.execute(
                "SELECT * FROM outcome_review_receipts WHERE outcome_id=? AND expected_revision=? AND action=?",
                (outcome_id, expected_revision, action),
            ).fetchone()
            if existing is not None:
                if existing["receipt_id"] != receipt_id or brief.state != result_state:
                    self._conflict()
                return ReviewCommitResult(brief=brief, receipt_id=receipt_id, idempotent=True)
            if brief.state not in ({OutcomeState.AWAITING_REVIEW} if action == "approve" else
                                   {OutcomeState.AWAITING_REVIEW, OutcomeState.APPROVED}):
                self._conflict()
            cursor = conn.execute(
                "UPDATE outcome_briefs SET state=?,updated_at=CURRENT_TIMESTAMP WHERE outcome_id=? AND revision=? AND content_sha256=? AND state=?",
                (result_state.value, outcome_id, expected_revision, content_sha256, brief.state.value),
            )
            if cursor.rowcount != 1:
                self._conflict()
            conn.execute(
                "INSERT INTO outcome_review_receipts (receipt_id,outcome_id,expected_revision,content_sha256,expected_input_revision,action,reason_code,result_state,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (receipt_id, outcome_id, expected_revision, content_sha256, expected_input_revision, action,
                 reason_code, result_state.value, datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")),
            )
            row = conn.execute("SELECT * FROM outcome_briefs WHERE outcome_id=?", (outcome_id,)).fetchone()
            return ReviewCommitResult(brief=self._with_current(self._brief_from_row(row), topic), receipt_id=receipt_id, idempotent=False)

    def set_brief_state(
        self,
        *,
        topic_id: str,
        state: OutcomeState,
    ) -> ResearchBriefRecord:
        self._require_write()
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if row is None:
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_NOT_FOUND,
                    "该主题尚未生成研究成果。",
                    stage="outcome_brief",
                    recoverable=False,
                )
            conn.execute(
                """
                UPDATE outcome_briefs
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE topic_id = ?
                """,
                (state.value, topic_id),
            )
            updated = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if updated is None:
                raise RuntimeError("outcome brief state更新后未找到记录")
            return self._brief_from_row(updated)

    def get_bundle_rows(
        self,
        topic_id: str,
    ) -> tuple[
        TopicRecord,
        tuple[EvidenceRecord, ...],
        tuple[ClaimRecord, ...],
        ResearchBriefRecord | None,
    ] | None:
        """Read a stable DB projection; source validity is evaluated by the service."""

        with self.get_connection() as conn:
            self._require_schema(conn)
            return self._bundle_from_connection(conn, topic_id)

    def _bundle_from_connection(self, conn: sqlite3.Connection, topic_id: str, *, verify: bool = True):
        """Use the caller's snapshot for input, pointer and receipt validation."""
        try:
            topic_row = conn.execute(
                "SELECT * FROM outcome_topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if topic_row is None:
                return None
            if verify:
                from src.storage.outcome_migration import input_digest_from_connection

                # Validate persisted values before DTO normalization can hide
                # malformed JSON, a REAL revision or a mismatched locator kind.
                if topic_row["input_digest"] != input_digest_from_connection(conn, dict(topic_row)):
                    raise ValueError("persisted input digest mismatch")
            evidence_rows = conn.execute(
                """
                SELECT * FROM outcome_evidence
                WHERE topic_id = ?
                ORDER BY ordinal ASC, evidence_id ASC
                """,
                (topic_id,),
            ).fetchall()
            claim_rows = conn.execute(
                """
                SELECT * FROM outcome_claims
                WHERE topic_id = ?
                ORDER BY ordinal ASC, claim_id ASC
                """,
                (topic_id,),
            ).fetchall()
            brief_row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            claims = tuple(
                self._claim_from_row(row, self._claim_links(conn, str(row["claim_id"])))
                for row in claim_rows
            )
            topic = self._topic_from_row(topic_row)
            evidence = tuple(self._evidence_from_row(row) for row in evidence_rows)
            brief = self._brief_from_row(brief_row) if brief_row is not None else None
            if verify:
                try:
                    if topic.input_digest != outcome_input_digest(topic=topic, evidence=evidence, claims=claims):
                        raise ValueError("input digest mismatch")
                    if brief is not None:
                        if brief.outcome_id != topic.outcome_id:
                            raise ValueError("outcome identity mismatch")
                        if brief.binding_state == "verified" and brief.publication_id != outcome_publication_id(
                            outcome_id=brief.outcome_id, candidate_revision=brief.revision, input_digest=brief.rendered_input_digest
                        ):
                            raise ValueError("publication identity mismatch")
                    receipts = conn.execute("SELECT * FROM outcome_review_receipts WHERE outcome_id=?", (topic.outcome_id,)).fetchall()
                    for receipt in receipts:
                        if receipt["receipt_id"] != outcome_review_receipt_id(**{name: receipt[name] for name in (
                            "outcome_id", "expected_revision", "content_sha256", "expected_input_revision", "action", "reason_code"
                        )}):
                            raise ValueError("receipt identity mismatch")
                except (ValueError, TypeError) as exc:
                    raise PKVRuntimeError(ErrorCode.DATABASE_SCHEMA_DRIFT, "成果输入或发布绑定无法验证。", stage="outcome_binding") from exc
            return topic, evidence, claims, self._with_current(brief, topic) if brief is not None else None
        except (ValueError, TypeError, KeyError) as exc:
            raise PKVRuntimeError(ErrorCode.DATABASE_SCHEMA_DRIFT, "成果输入或发布绑定无法验证。", stage="outcome_binding") from exc


__all__ = ["OutcomeStore"]
