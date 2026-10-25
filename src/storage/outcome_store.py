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
    TopicRecord,
    TopicState,
    normalize_limitation_codes,
    normalize_outcome_text,
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
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _require_schema(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        present = {str(row[0]) for row in rows}
        if not _REQUIRED_TABLES.issubset(present):
            raise PKVRuntimeError(
                ErrorCode.DATABASE_UPGRADE_REQUIRED,
                "知识库需要升级后才能使用可追溯成果。",
                stage="outcome_schema",
                recoverable=True,
            )

    def _require_write(self) -> None:
        require_active_data_root_writer(self.layout, owner="knowledge_outcomes")

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
        )

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            evidence_id=str(row["evidence_id"]),
            topic_id=str(row["topic_id"]),
            ordinal=int(row["ordinal"]),
            locator=EvidenceLocator.parse(str(row["locator"])),
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
            ordinal=int(row["ordinal"]),
            statement=str(row["statement"]),
            state=ClaimState(str(row["state"])),
            limitation_codes=normalize_limitation_codes(
                json.loads(str(row["limitation_codes"]))
            ),
            revision=int(row["revision"]),
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

    @staticmethod
    def _touch_topic(conn: sqlite3.Connection, topic_id: str) -> None:
        conn.execute(
            """
            UPDATE outcome_topics
            SET revision = revision + 1, updated_at = CURRENT_TIMESTAMP
            WHERE topic_id = ?
            """,
            (topic_id,),
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
            conn.execute(
                """
                INSERT INTO outcome_topics (topic_id, title, question, state)
                VALUES (?, ?, ?, ?)
                """,
                (topic_id, clean_title, clean_question, TopicState.ACTIVE.value),
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
                SET next_evidence_ordinal = ?, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE topic_id = ?
                """,
                (ordinal + 1, topic_id),
            )
            row = self._evidence_or_raise(conn, evidence_id)
            return self._evidence_from_row(row)

    def delete_evidence(self, evidence_id: str) -> str:
        """Detach one selected evidence item and return its owning topic id."""

        self._require_write()
        with self.get_connection() as conn:
            self._require_schema(conn)
            evidence = self._evidence_or_raise(conn, evidence_id)
            topic_id = str(evidence["topic_id"])
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
            for evidence_id, state in updates.items():
                if type(state) is not EvidenceState:
                    raise TypeError("evidence state无效")
                row = self._evidence_or_raise(conn, evidence_id)
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
                SET next_claim_ordinal = ?, revision = revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE topic_id = ?
                """,
                (ordinal + 1, topic_id),
            )
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
            evidence = self._evidence_or_raise(conn, evidence_id)
            if str(claim["topic_id"]) != str(evidence["topic_id"]):
                raise PKVRuntimeError(
                    ErrorCode.OUTCOME_INVALID,
                    "主张和证据必须属于同一研究主题。",
                    stage="outcome_claim_evidence",
                    recoverable=False,
                )
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
            return self._brief_from_row(row) if row is not None else None

    def get_brief(self, outcome_id: str) -> ResearchBriefRecord | None:
        with self.get_connection() as conn:
            self._require_schema(conn)
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE outcome_id = ?", (outcome_id,)
            ).fetchone()
            return self._brief_from_row(row) if row is not None else None

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
    ) -> ResearchBriefRecord:
        """Commit a pointer to an already atomically published immutable Markdown file."""

        self._require_write()
        if type(revision) is not int or revision < 1:
            raise ValueError("outcome revision无效")
        with self.get_connection() as conn:
            self._require_schema(conn)
            self._topic_or_raise(conn, topic_id)
            existing = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if existing is None:
                if expected_previous_revision is not None or revision != 1:
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
                        markdown_relative_path, content_sha256
                    ) VALUES (?, ?, 'research_brief', ?, ?, ?, ?)
                    """,
                    (
                        outcome_id,
                        topic_id,
                        OutcomeState.AWAITING_REVIEW.value,
                        revision,
                        markdown_relative_path,
                        content_sha256,
                    ),
                )
            else:
                previous_revision = int(existing["revision"])
                if (
                    expected_previous_revision != previous_revision
                    or revision != previous_revision + 1
                    or str(existing["outcome_id"]) != outcome_id
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
                        content_sha256 = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE topic_id = ?
                    """,
                    (
                        OutcomeState.AWAITING_REVIEW.value,
                        revision,
                        markdown_relative_path,
                        content_sha256,
                        topic_id,
                    ),
                )
            row = conn.execute(
                "SELECT * FROM outcome_briefs WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("outcome brief upsert后未找到记录")
            return self._brief_from_row(row)

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
            topic_row = conn.execute(
                "SELECT * FROM outcome_topics WHERE topic_id = ?", (topic_id,)
            ).fetchone()
            if topic_row is None:
                return None
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
            return (
                self._topic_from_row(topic_row),
                tuple(self._evidence_from_row(row) for row in evidence_rows),
                claims,
                self._brief_from_row(brief_row) if brief_row is not None else None,
            )


__all__ = ["OutcomeStore"]
