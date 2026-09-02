"""R4 Q2 token reservation and usage ledger.

This deliberately does not reuse the pre-R4 candidate tables: their foreign
keys and claim contract belong to ``ai_automation_tasks``.  Every row here is
fenced to the new Q2 task identity, separates reservation/local estimate from
Provider facts, and leaves unknown Provider fields as SQL NULL.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from src.runtime.ai_automation_policy import TokenUsage
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer
from src.storage.content_lifecycle import AIDerivationTask
from src.storage.sqlite_connection import connect_existing_sqlite


_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY_PATTERN = re.compile(r"[A-Z]{3}\Z")
_STAGE = "r4_derivation_ledger"


def _require_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 无效")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是 sha256")
    return value


def _require_optional_nonnegative(value: object, *, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} 必须是非负整数或 null")
    return value


def _require_currency(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _CURRENCY_PATTERN.fullmatch(value) is None:
        raise ValueError("currency 无效")
    return value


@dataclass(frozen=True)
class DerivationReservation:
    reservation_id: str
    task_id: str
    claim_token: str
    owner_fence: int
    policy_fingerprint: str
    timezone: str
    local_day: str
    local_month: str
    reserved_tokens: int
    reserved_micros: int | None
    currency: str | None
    state: str


class DerivationLedger:
    """Transactional reservation/usage operations under an active R3 lease."""

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._db_path = Path(layout.db_path)

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        require_active_data_root_writer(self._layout, owner="r4_derivation_ledger")
        try:
            connection = connect_existing_sqlite(self._db_path)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        except PKVRuntimeError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "R4 Q2 用量账本无法持久化。",
                stage=_STAGE,
                recoverable=True,
            ) from exc

    @staticmethod
    def _require_active_claim(task: AIDerivationTask) -> tuple[str, int, str]:
        if task.claim_token is None or task.owner_fence <= 0 or task.policy_fingerprint is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q2 用量账本需要当前 policy-bound claim。",
                stage=_STAGE,
                recoverable=True,
            )
        return (
            _require_id(task.claim_token, label="claim_token"),
            task.owner_fence,
            _require_sha256(task.policy_fingerprint, label="policy_fingerprint"),
        )

    def reserve(
        self,
        task: AIDerivationTask,
        *,
        timezone: str,
        local_day: str,
        local_month: str,
        reserved_tokens: int,
        daily_total_tokens: int | None,
        monthly_total_tokens: int | None,
        reserved_micros: int | None = None,
        daily_cap_micros: int | None = None,
        monthly_cap_micros: int | None = None,
        currency: str | None = None,
    ) -> DerivationReservation | None:
        """Reserve caps before Provider construction; None means paused."""

        token, fence, policy = self._require_active_claim(task)
        if not isinstance(timezone, str) or not timezone:
            raise ValueError("timezone 无效")
        if not isinstance(local_day, str) or not local_day:
            raise ValueError("local_day 无效")
        if not isinstance(local_month, str) or not local_month:
            raise ValueError("local_month 无效")
        if type(reserved_tokens) is not int or reserved_tokens < 0:
            raise ValueError("reserved_tokens 必须是非负整数")
        reserved_micros = _require_optional_nonnegative(
            reserved_micros, label="reserved_micros"
        )
        daily_cap_micros = _require_optional_nonnegative(
            daily_cap_micros, label="daily_cap_micros"
        )
        monthly_cap_micros = _require_optional_nonnegative(
            monthly_cap_micros, label="monthly_cap_micros"
        )
        currency = _require_currency(currency)
        if reserved_micros is not None and currency is None:
            raise ValueError("金额 reservation 必须声明 currency")
        if (daily_cap_micros is not None or monthly_cap_micros is not None) and (
            reserved_micros is None or currency is None
        ):
            raise ValueError("金额上限需要可计算的 reservation")

        with self._write_transaction() as connection:
            task_row = connection.execute(
                """
                SELECT state, claim_token, owner_fence, policy_fingerprint,
                       claimed_until > CURRENT_TIMESTAMP AS claim_live
                FROM ai_derivation_tasks WHERE task_id = ?
                """,
                (task.task_id,),
            ).fetchone()
            if (
                task_row is None
                or task_row["state"] != "processing"
                or task_row["claim_token"] != token
                or task_row["owner_fence"] != fence
                or task_row["policy_fingerprint"] != policy
                or task_row["claim_live"] != 1
            ):
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 claim 在预算预留前已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            # Calculate daily and monthly separately so a day cannot spend an
            # entire month cap.
            daily = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_tokens), 0), COALESCE(SUM(reserved_micros), 0)
                FROM ai_derivation_reservations
                WHERE policy_fingerprint = ? AND timezone = ? AND local_day = ?
                  AND state IN ('reserved', 'settled')
                """,
                (policy, timezone, local_day),
            ).fetchone()
            monthly = connection.execute(
                """
                SELECT COALESCE(SUM(reserved_tokens), 0), COALESCE(SUM(reserved_micros), 0)
                FROM ai_derivation_reservations
                WHERE policy_fingerprint = ? AND timezone = ? AND local_month = ?
                  AND state IN ('reserved', 'settled')
                """,
                (policy, timezone, local_month),
            ).fetchone()
            if (
                daily_total_tokens is not None
                and int(daily[0]) + reserved_tokens > daily_total_tokens
            ) or (
                monthly_total_tokens is not None
                and int(monthly[0]) + reserved_tokens > monthly_total_tokens
            ) or (
                daily_cap_micros is not None
                and int(daily[1]) + int(reserved_micros or 0) > daily_cap_micros
            ) or (
                monthly_cap_micros is not None
                and int(monthly[1]) + int(reserved_micros or 0) > monthly_cap_micros
            ):
                return None
            reservation_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO ai_derivation_reservations(
                    reservation_id, task_id, claim_token, owner_fence,
                    policy_fingerprint, timezone, local_day, local_month,
                    reserved_tokens, reserved_micros, currency, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved')
                """,
                (
                    reservation_id,
                    task.task_id,
                    token,
                    fence,
                    policy,
                    timezone,
                    local_day,
                    local_month,
                    reserved_tokens,
                    reserved_micros,
                    currency,
                ),
            )
            return DerivationReservation(
                reservation_id,
                task.task_id,
                token,
                fence,
                policy,
                timezone,
                local_day,
                local_month,
                reserved_tokens,
                reserved_micros,
                currency,
                "reserved",
            )

    def record_usage(
        self,
        task: AIDerivationTask,
        *,
        reservation_id: str | None,
        stage: str,
        usage: TokenUsage,
        amount_micros: int | None = None,
        currency: str | None = None,
    ) -> str:
        """Append a fact; missing Provider dimensions remain NULL."""

        token, fence, _ = self._require_active_claim(task)
        if stage not in {"summary", "tags", "embedding"}:
            raise ValueError("usage stage 无效")
        if not isinstance(usage, TokenUsage):
            raise TypeError("usage 必须是 TokenUsage")
        if reservation_id is not None:
            reservation_id = _require_id(reservation_id, label="reservation_id")
        amount_micros = _require_optional_nonnegative(amount_micros, label="amount_micros")
        currency = _require_currency(currency)
        if amount_micros is not None and currency is None:
            raise ValueError("amount_micros 必须声明 currency")
        usage_id = uuid.uuid4().hex
        with self._write_transaction() as connection:
            task_row = connection.execute(
                """
                SELECT state, claim_token, owner_fence,
                       claimed_until > CURRENT_TIMESTAMP AS claim_live
                FROM ai_derivation_tasks
                WHERE task_id = ?
                """,
                (task.task_id,),
            ).fetchone()
            if (
                task_row is None
                or task_row["state"] != "processing"
                or task_row["claim_token"] != token
                or task_row["owner_fence"] != fence
                or task_row["claim_live"] != 1
            ):
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 claim 在 usage 回填前已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            connection.execute(
                """
                INSERT INTO ai_derivation_usage(
                    usage_id, task_id, reservation_id, stage, source,
                    uncached_input_tokens, cached_input_tokens, generated_tokens,
                    embedding_input_tokens, amount_micros, currency
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_id,
                    task.task_id,
                    reservation_id,
                    stage,
                    usage.source,
                    usage.uncached_input_tokens,
                    usage.cached_input_tokens,
                    usage.generated_tokens,
                    usage.embedding_input_tokens,
                    amount_micros,
                    currency,
                ),
            )
        return usage_id

    def settle(
        self,
        reservation: DerivationReservation,
        *,
        settled_tokens: int | None = None,
        settled_micros: int | None = None,
    ) -> None:
        settled_tokens = _require_optional_nonnegative(
            settled_tokens, label="settled_tokens"
        )
        settled_micros = _require_optional_nonnegative(
            settled_micros, label="settled_micros"
        )
        with self._write_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE ai_derivation_reservations
                SET state = 'settled', settled_tokens = ?, settled_micros = ?,
                    settled_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ? AND state = 'reserved'
                  AND claim_token = ? AND owner_fence = ?
                  AND EXISTS (
                      SELECT 1 FROM ai_derivation_tasks task
                      WHERE task.task_id = ai_derivation_reservations.task_id
                        AND task.state = 'processing'
                        AND task.claim_token = ai_derivation_reservations.claim_token
                        AND task.owner_fence = ai_derivation_reservations.owner_fence
                        AND task.claimed_until > CURRENT_TIMESTAMP
                  )
                """,
                (
                    settled_tokens,
                    settled_micros,
                    reservation.reservation_id,
                    reservation.claim_token,
                    reservation.owner_fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 reservation 结算 claim 已失效。",
                    stage=_STAGE,
                    recoverable=True,
                )

    def release(self, reservation: DerivationReservation) -> None:
        with self._write_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE ai_derivation_reservations
                SET state = 'released', settled_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ? AND state = 'reserved'
                  AND claim_token = ? AND owner_fence = ?
                  AND EXISTS (
                      SELECT 1 FROM ai_derivation_tasks task
                      WHERE task.task_id = ai_derivation_reservations.task_id
                        AND task.state = 'processing'
                        AND task.claim_token = ai_derivation_reservations.claim_token
                        AND task.owner_fence = ai_derivation_reservations.owner_fence
                        AND task.claimed_until > CURRENT_TIMESTAMP
                  )
                """,
                (
                    reservation.reservation_id,
                    reservation.claim_token,
                    reservation.owner_fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 reservation 释放 claim 已失效。",
                    stage=_STAGE,
                    recoverable=True,
                )

    def settle_detached(
        self,
        reservation: DerivationReservation,
        *,
        settled_tokens: int | None = None,
        settled_micros: int | None = None,
    ) -> bool:
        """Conservatively settle a billed reservation after its claim was fenced.

        A late Provider response cannot alter a superseding task, content patch,
        or generation binding.  Its already-reserved amount still counts toward
        caps, however, so the original reservation may be settled by its own
        immutable token/fence even after the task claim is no longer live.
        ``False`` is idempotent: another recovery path already settled/released
        the same reservation.
        """

        settled_tokens = _require_optional_nonnegative(
            reservation.reserved_tokens if settled_tokens is None else settled_tokens,
            label="settled_tokens",
        )
        settled_micros = _require_optional_nonnegative(
            reservation.reserved_micros if settled_micros is None else settled_micros,
            label="settled_micros",
        )
        with self._write_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE ai_derivation_reservations
                SET state = 'settled', settled_tokens = ?, settled_micros = ?,
                    settled_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ? AND state = 'reserved'
                  AND claim_token = ? AND owner_fence = ?
                """,
                (
                    settled_tokens,
                    settled_micros,
                    reservation.reservation_id,
                    reservation.claim_token,
                    reservation.owner_fence,
                ),
            ).rowcount
        return changed == 1

    def release_detached(self, reservation: DerivationReservation) -> bool:
        """Release an unbilled reservation after a claim has been fenced."""

        with self._write_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE ai_derivation_reservations
                SET state = 'released', settled_at = CURRENT_TIMESTAMP
                WHERE reservation_id = ? AND state = 'reserved'
                  AND claim_token = ? AND owner_fence = ?
                """,
                (
                    reservation.reservation_id,
                    reservation.claim_token,
                    reservation.owner_fence,
                ),
            ).rowcount
        return changed == 1


__all__ = ["DerivationLedger", "DerivationReservation"]
