"""Lease-protected durable state for the internal R4 AI lifecycle.

The store contains no Provider factory and no background worker.  It is a
narrow SQLite port for a future Application-owned scheduler: durable task
claiming, token reservation, usage facts, and conservative settlement.  Every
mutation verifies the current task's R3 lease before opening a writable SQLite
connection, while read methods use SQLite's readonly connection mode.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator

from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer
from src.storage.sqlite_connection import connect_existing_sqlite


_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_MUTATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_MONTH_PATTERN = re.compile(r"\d{4}-\d{2}\Z")


class AIAutomationTaskState(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_REQUIRED = "retry_required"
    BUDGET_PAUSED = "budget_paused"
    AUTHORIZATION_REQUIRED = "authorization_required"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class AIAutomationTask:
    task_id: str
    mutation_id: str
    source_digest: str
    policy_fingerprint: str
    state: AIAutomationTaskState
    attempt_count: int
    claim_token: str | None = None
    last_error_code: str | None = None


@dataclass(frozen=True)
class TokenReservation:
    reservation_id: str
    task_id: str
    policy_fingerprint: str
    timezone: str
    local_day: str
    local_month: str
    reserved_tokens: int
    settled_tokens: int | None
    state: str


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是 sha256")
    return value


def _require_positive_token_count(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} 必须是非负整数")
    return value


def _optional_token_cap(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    return _require_positive_token_count(value, label=label)


def _require_claim_token(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("claim token 无效")
    return value


def _task_from_row(row: sqlite3.Row) -> AIAutomationTask:
    try:
        return AIAutomationTask(
            task_id=str(row["task_id"]),
            mutation_id=str(row["mutation_id"]),
            source_digest=_require_sha256(row["source_digest"], label="source_digest"),
            policy_fingerprint=_require_sha256(
                row["policy_fingerprint"], label="policy_fingerprint"
            ),
            state=AIAutomationTaskState(row["state"]),
            attempt_count=_require_positive_token_count(
                row["attempt_count"], label="attempt_count"
            ),
            claim_token=row["claim_token"],
            last_error_code=row["last_error_code"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "AI 自动化任务账本记录不可验证。",
            stage="ai_automation_ledger",
            recoverable=True,
        ) from exc


def _reservation_from_row(row: sqlite3.Row) -> TokenReservation:
    try:
        state = row["state"]
        if state not in {"reserved", "settled", "released"}:
            raise ValueError("reservation state")
        return TokenReservation(
            reservation_id=str(row["reservation_id"]),
            task_id=str(row["task_id"]),
            policy_fingerprint=_require_sha256(
                row["policy_fingerprint"], label="policy_fingerprint"
            ),
            timezone=str(row["timezone"]),
            local_day=str(row["local_day"]),
            local_month=str(row["local_month"]),
            reserved_tokens=_require_positive_token_count(
                row["reserved_tokens"], label="reserved_tokens"
            ),
            settled_tokens=(
                _require_positive_token_count(row["settled_tokens"], label="settled_tokens")
                if row["settled_tokens"] is not None
                else None
            ),
            state=state,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "AI token reservation 账本记录不可验证。",
            stage="ai_automation_ledger",
            recoverable=True,
        ) from exc


class AIAutomationTaskStore:
    """Internal durable port; only an Application mutation owner may mutate it."""

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._db_path = layout.db_path

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = connect_existing_sqlite(self._db_path, read_only=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except sqlite3.Error as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "AI 自动化账本无法以只读方式读取。",
                stage="ai_automation_ledger",
                recoverable=True,
            ) from exc
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        require_active_data_root_writer(self._layout, owner="ai_automation_lifecycle")
        connection = connect_existing_sqlite(self._db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except PKVRuntimeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "AI 自动化账本无法持久化。",
                stage="ai_automation_ledger",
                recoverable=True,
            ) from exc
        finally:
            connection.close()

    @staticmethod
    def _require_mutation_id(mutation_id: object) -> str:
        if not isinstance(mutation_id, str) or _MUTATION_ID_PATTERN.fullmatch(mutation_id) is None:
            raise ValueError("mutation_id 无效")
        return mutation_id

    @staticmethod
    def _require_error_code(error_code: object) -> str | None:
        if error_code is None:
            return None
        if not isinstance(error_code, str) or _ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("error_code 无效")
        return error_code

    def get_task(self, task_id: str) -> AIAutomationTask | None:
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def enqueue_rebuild(
        self,
        *,
        mutation_id: str,
        source_digest: str,
        policy_fingerprint: str,
        state: AIAutomationTaskState = AIAutomationTaskState.PENDING,
    ) -> AIAutomationTask:
        """Durably enqueue once per mutation; repeated calls are idempotent."""

        mutation_id = self._require_mutation_id(mutation_id)
        source_digest = _require_sha256(source_digest, label="source_digest")
        policy_fingerprint = _require_sha256(
            policy_fingerprint, label="policy_fingerprint"
        )
        if state not in {
            AIAutomationTaskState.PENDING,
            AIAutomationTaskState.BUDGET_PAUSED,
            AIAutomationTaskState.AUTHORIZATION_REQUIRED,
        }:
            raise ValueError("新 AI 自动化任务必须从可调度或暂停状态入队")
        with self._write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks WHERE mutation_id = ?
                """,
                (mutation_id,),
            ).fetchone()
            if existing is not None:
                task = _task_from_row(existing)
                if (
                    task.source_digest != source_digest
                    or task.policy_fingerprint != policy_fingerprint
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "同一业务 mutation 的 AI 自动化任务参数已变化。",
                        stage="ai_automation_ledger",
                        recoverable=True,
                    )
                return task
            task_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO ai_automation_tasks(
                    task_id, mutation_id, source_digest, policy_fingerprint, state
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, mutation_id, source_digest, policy_fingerprint, state.value),
            )
            row = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            assert row is not None
            return _task_from_row(row)

    def claim_next(self) -> AIAutomationTask | None:
        """Atomically claim one pending/retry task under the already-held root lease."""

        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT task_id FROM ai_automation_tasks
                WHERE state IN (?, ?)
                ORDER BY created_at ASC, task_id ASC
                LIMIT 1
                """,
                (
                    AIAutomationTaskState.PENDING.value,
                    AIAutomationTaskState.RETRY_REQUIRED.value,
                ),
            ).fetchone()
            if row is None:
                return None
            task_id = str(row["task_id"])
            claim_token = uuid.uuid4().hex
            changed = connection.execute(
                """
                UPDATE ai_automation_tasks
                SET state = ?, attempt_count = attempt_count + 1, claim_token = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN (?, ?)
                """,
                (
                    AIAutomationTaskState.PROCESSING.value,
                    claim_token,
                    task_id,
                    AIAutomationTaskState.PENDING.value,
                    AIAutomationTaskState.RETRY_REQUIRED.value,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "AI 自动化任务在 claim 前已变化。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            claimed = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            assert claimed is not None
            return _task_from_row(claimed)

    def mark_retry(
        self,
        task_id: str,
        *,
        claim_token: str,
        error_code: str,
    ) -> AIAutomationTask:
        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            state=AIAutomationTaskState.RETRY_REQUIRED,
            error_code=error_code,
        )

    def mark_completed(self, task_id: str, *, claim_token: str) -> AIAutomationTask:
        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            state=AIAutomationTaskState.COMPLETED,
            error_code=None,
        )

    def mark_superseded(self, task_id: str, *, claim_token: str) -> AIAutomationTask:
        """Terminally retire a claim whose captured source is no longer current.

        A later committed archive/delete owns the new source digest and task.  An
        older task must never rebuild the newer Vault or remain first in FIFO
        order forever, so this transition is distinct from a provider retry.
        """

        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            state=AIAutomationTaskState.SUPERSEDED,
            error_code=ErrorCode.RUNTIME_PLAN_STALE.value,
        )

    def mark_budget_paused(
        self, task_id: str, *, claim_token: str
    ) -> AIAutomationTask:
        """Release a live claim because its approved token cap is exhausted."""

        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            state=AIAutomationTaskState.BUDGET_PAUSED,
            error_code=ErrorCode.EMBEDDING_BUDGET_PAUSED.value,
        )

    def mark_authorization_required(
        self, task_id: str, *, claim_token: str
    ) -> AIAutomationTask:
        """Release a live claim after a policy re-check loses authorization."""

        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            state=AIAutomationTaskState.AUTHORIZATION_REQUIRED,
            error_code=ErrorCode.EMBEDDING_AUTOMATION_AUTHORIZATION_REQUIRED.value,
        )

    def resume_paused_task(
        self,
        task_id: str,
        *,
        source_digest: str,
        policy_fingerprint: str,
    ) -> AIAutomationTask:
        """Requeue a paused task only after the Application re-checks its policy.

        This port deliberately cannot decide that a changed Provider/model or
        token policy is authorized.  Its caller must first run the pure policy
        inspector and obtain a ``READY`` result.  The source digest remains
        immutable so an unrelated archive/delete mutation cannot be hidden by a
        policy resume.
        """

        source_digest = _require_sha256(source_digest, label="source_digest")
        policy_fingerprint = _require_sha256(
            policy_fingerprint, label="policy_fingerprint"
        )
        with self._write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if existing is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "待恢复的 AI 自动化任务不存在。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            task = _task_from_row(existing)
            if task.source_digest != source_digest or task.state not in {
                AIAutomationTaskState.BUDGET_PAUSED,
                AIAutomationTaskState.AUTHORIZATION_REQUIRED,
            }:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "待恢复的 AI 自动化任务已变化。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            connection.execute(
                """
                UPDATE ai_automation_tasks
                SET policy_fingerprint = ?, state = ?, claim_token = NULL,
                    last_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (policy_fingerprint, AIAutomationTaskState.PENDING.value, task_id),
            )
            row = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            assert row is not None
            return _task_from_row(row)

    def _finish_claim(
        self,
        task_id: str,
        *,
        claim_token: str,
        state: AIAutomationTaskState,
        error_code: str | None,
    ) -> AIAutomationTask:
        token = _require_claim_token(claim_token)
        error_code = self._require_error_code(error_code)
        with self._write_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE ai_automation_tasks
                SET state = ?, claim_token = NULL, last_error_code = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ?
                """,
                (
                    state.value,
                    error_code,
                    task_id,
                    AIAutomationTaskState.PROCESSING.value,
                    token,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "AI 自动化任务的 lease/claim 已失效。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            row = connection.execute(
                """
                SELECT task_id, mutation_id, source_digest, policy_fingerprint,
                       state, attempt_count, claim_token, last_error_code
                FROM ai_automation_tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            assert row is not None
            return _task_from_row(row)

    def reserve_tokens(
        self,
        task_id: str,
        *,
        claim_token: str,
        timezone: str,
        local_day: str,
        local_month: str,
        reserved_tokens: int,
        daily_total_tokens: int | None,
        monthly_total_tokens: int | None,
    ) -> TokenReservation | None:
        """Reserve conservative capacity, or atomically pause before Provider work.

        ``None`` means the ledger committed a ``budget_paused`` task transition;
        callers must not construct a Provider in that case.  Checking current
        usage and inserting the reservation happen in the same transaction so
        a future scheduler cannot accidentally turn the read-side status API
        into a race-prone budget gate.
        """

        token = _require_claim_token(claim_token)
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone 无效")
        if not isinstance(local_day, str) or _DAY_PATTERN.fullmatch(local_day) is None:
            raise ValueError("local_day 无效")
        if not isinstance(local_month, str) or _MONTH_PATTERN.fullmatch(local_month) is None:
            raise ValueError("local_month 无效")
        reserved_tokens = _require_positive_token_count(
            reserved_tokens, label="reserved_tokens"
        )
        daily_total_tokens = _optional_token_cap(
            daily_total_tokens, label="daily_total_tokens"
        )
        monthly_total_tokens = _optional_token_cap(
            monthly_total_tokens, label="monthly_total_tokens"
        )
        if daily_total_tokens is None and monthly_total_tokens is None:
            raise ValueError("token reservation 至少需要一个 hard cap")
        with self._write_transaction() as connection:
            task = connection.execute(
                """
                SELECT task_id, policy_fingerprint FROM ai_automation_tasks
                WHERE task_id = ? AND state = ? AND claim_token = ?
                """,
                (task_id, AIAutomationTaskState.PROCESSING.value, token),
            ).fetchone()
            if task is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "AI 自动化任务不能由过期 claim 预留 token。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            day_total = self._token_total_in_transaction(
                connection,
                policy_fingerprint=str(task["policy_fingerprint"]),
                timezone=timezone,
                column="local_day",
                period=local_day,
            )
            month_total = self._token_total_in_transaction(
                connection,
                policy_fingerprint=str(task["policy_fingerprint"]),
                timezone=timezone,
                column="local_month",
                period=local_month,
            )
            if (
                daily_total_tokens == 0
                or monthly_total_tokens == 0
                or (
                    daily_total_tokens is not None
                    and day_total + reserved_tokens > daily_total_tokens
                )
                or (
                    monthly_total_tokens is not None
                    and month_total + reserved_tokens > monthly_total_tokens
                )
            ):
                connection.execute(
                    """
                    UPDATE ai_automation_tasks
                    SET state = ?, claim_token = NULL, last_error_code = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = ? AND state = ? AND claim_token = ?
                    """,
                    (
                        AIAutomationTaskState.BUDGET_PAUSED.value,
                        ErrorCode.EMBEDDING_BUDGET_PAUSED.value,
                        task_id,
                        AIAutomationTaskState.PROCESSING.value,
                        token,
                    ),
                )
                return None
            reservation_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO ai_token_reservations(
                    reservation_id, task_id, claim_token, policy_fingerprint, timezone,
                    local_day, local_month, reserved_tokens, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved')
                """,
                (
                    reservation_id,
                    task_id,
                    token,
                    task["policy_fingerprint"],
                    timezone,
                    local_day,
                    local_month,
                    reserved_tokens,
                ),
            )
            row = connection.execute(
                """
                SELECT reservation_id, task_id, policy_fingerprint, timezone,
                       local_day, local_month, reserved_tokens, settled_tokens, state
                FROM ai_token_reservations WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            assert row is not None
            return _reservation_from_row(row)

    @staticmethod
    def _token_total_in_transaction(
        connection: sqlite3.Connection,
        *,
        policy_fingerprint: str,
        timezone: str,
        column: str,
        period: str,
    ) -> int:
        if column not in {"local_day", "local_month"}:
            raise ValueError("预算 period 列无效")
        row = connection.execute(
            f"""
            SELECT COALESCE(SUM(
                CASE WHEN state = 'settled' THEN settled_tokens ELSE reserved_tokens END
            ), 0) AS total_tokens
            FROM ai_token_reservations
            WHERE policy_fingerprint = ? AND timezone = ? AND {column} = ?
              AND state IN ('reserved', 'settled')
            """,
            (policy_fingerprint, timezone, period),
        ).fetchone()
        return int(row["total_tokens"] if row is not None else 0)

    def record_usage(
        self,
        task_id: str,
        *,
        claim_token: str,
        usage: TokenUsage,
        reservation_id: str | None = None,
    ) -> str:
        """Persist token facts without changing unknown provider fields to zero."""

        if not isinstance(usage, TokenUsage):
            raise TypeError("usage 必须是 TokenUsage")
        token = _require_claim_token(claim_token)
        with self._write_transaction() as connection:
            task = connection.execute(
                """
                SELECT task_id FROM ai_automation_tasks
                WHERE task_id = ? AND state = ? AND claim_token = ?
                """,
                (task_id, AIAutomationTaskState.PROCESSING.value, token),
            ).fetchone()
            if task is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "AI token 用量不能写入过期 claim。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            if reservation_id is not None:
                reservation = connection.execute(
                    """
                    SELECT reservation_id FROM ai_token_reservations
                    WHERE reservation_id = ? AND task_id = ? AND claim_token = ?
                      AND state = 'reserved'
                    """,
                    (reservation_id, task_id, token),
                ).fetchone()
                if reservation is None:
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "AI token reservation 已失效。",
                        stage="ai_automation_ledger",
                        recoverable=True,
                    )
            usage_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO ai_token_usage(
                    usage_id, task_id, reservation_id, source,
                    uncached_input_tokens, cached_input_tokens,
                    generated_tokens, embedding_input_tokens
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    task_id,
                    reservation_id,
                    usage.source,
                    usage.uncached_input_tokens,
                    usage.cached_input_tokens,
                    usage.generated_tokens,
                    usage.embedding_input_tokens,
                ),
            )
            return usage_id

    def settle_reservation(
        self,
        reservation_id: str,
        *,
        claim_token: str,
    ) -> TokenReservation:
        """Settle conservatively: unknown usage can never lower the reservation."""

        token = _require_claim_token(claim_token)
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT r.reservation_id, r.task_id, r.policy_fingerprint, r.timezone,
                       r.local_day, r.local_month, r.reserved_tokens, r.settled_tokens, r.state
                FROM ai_token_reservations AS r
                JOIN ai_automation_tasks AS t ON t.task_id = r.task_id
                WHERE r.reservation_id = ? AND r.state = 'reserved'
                  AND r.claim_token = ? AND t.state = ? AND t.claim_token = ?
                """,
                (
                    reservation_id,
                    token,
                    AIAutomationTaskState.PROCESSING.value,
                    token,
                ),
            ).fetchone()
            if row is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "AI token reservation 不能由过期 claim 结算。",
                    stage="ai_automation_ledger",
                    recoverable=True,
                )
            reservation = _reservation_from_row(row)
            usage_row = connection.execute(
                """
                SELECT
                    COALESCE(SUM(COALESCE(uncached_input_tokens, 0)), 0)
                    + COALESCE(SUM(COALESCE(cached_input_tokens, 0)), 0)
                    + COALESCE(SUM(COALESCE(generated_tokens, 0)), 0)
                    + COALESCE(SUM(COALESCE(embedding_input_tokens, 0)), 0)
                    AS known_tokens
                FROM ai_token_usage
                WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            known_tokens = int(usage_row["known_tokens"] if usage_row is not None else 0)
            settled_tokens = max(reservation.reserved_tokens, known_tokens)
            connection.execute(
                """
                UPDATE ai_token_reservations
                SET state = 'settled', settled_tokens = ?, settled_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ? AND state = 'reserved'
                """,
                (settled_tokens, reservation_id),
            )
            settled = connection.execute(
                """
                SELECT reservation_id, task_id, policy_fingerprint, timezone,
                       local_day, local_month, reserved_tokens, settled_tokens, state
                FROM ai_token_reservations WHERE reservation_id = ?
                """,
                (reservation_id,),
            ).fetchone()
            assert settled is not None
            return _reservation_from_row(settled)

    def token_total(
        self,
        *,
        policy_fingerprint: str,
        timezone: str,
        local_day: str | None = None,
        local_month: str | None = None,
    ) -> int:
        """Return settled-or-reserved capacity for exactly one local day or month."""

        policy_fingerprint = _require_sha256(
            policy_fingerprint, label="policy_fingerprint"
        )
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone 无效")
        if (local_day is None) == (local_month is None):
            raise ValueError("必须精确指定 local_day 或 local_month")
        column = "local_day" if local_day is not None else "local_month"
        period = local_day if local_day is not None else local_month
        pattern = _DAY_PATTERN if local_day is not None else _MONTH_PATTERN
        if not isinstance(period, str) or pattern.fullmatch(period) is None:
            raise ValueError("预算 period 无效")
        with self._read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT COALESCE(SUM(
                    CASE WHEN state = 'settled' THEN settled_tokens ELSE reserved_tokens END
                ), 0) AS total_tokens
                FROM ai_token_reservations
                WHERE policy_fingerprint = ? AND timezone = ? AND {column} = ?
                  AND state IN ('reserved', 'settled')
                """,
                (policy_fingerprint, timezone, period),
            ).fetchone()
        return int(row["total_tokens"] if row is not None else 0)


__all__ = [
    "AIAutomationTask",
    "AIAutomationTaskState",
    "AIAutomationTaskStore",
    "TokenReservation",
]
