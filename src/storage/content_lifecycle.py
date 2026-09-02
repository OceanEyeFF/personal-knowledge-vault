"""Durable R4 Q1′ content tasks, handoffs, and private prepared payloads.

The module deliberately contains no crawler, workflow engine, Provider, vector
writer, or public adapter.  It is the persistence boundary for the new R4
pipeline:

``PreparedDocument spool -> ContentMutationTask (Q1′) -> durable handoff ->
blocked/activated AIDerivationTask (Q2)``.

Document bodies are only held in the private, immutable spool.  SQLite retains
references, hashes, lifecycle state, and no credential-bearing Provider data.
Every mutation requires the caller's active R3 data-root writer lease.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import (
    atomic_publish_file,
    ensure_safe_directory,
    open_user_file_nofollow,
    validate_directory_components,
)
from src.runtime.writer_inventory import require_active_data_root_writer
from src.storage.markdown_store import Entry
from src.storage.sqlite_connection import connect_existing_sqlite


_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_ENTRY_FIELD_NAMES = frozenset(field.name for field in fields(Entry))
_SPOOL_SCHEMA_VERSION = 1
_STAGE = "r4_content_lifecycle"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 无效")
    return value


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} 必须是 sha256")
    return value


def _require_error_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _ERROR_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError("error_code 无效")
    return value


def _require_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return value


class ContentMutationAction(str, Enum):
    ARCHIVE = "archive"
    DELETE = "delete"
    APPLY_AI_PATCH = "apply_ai_patch"


class ContentMutationState(str, Enum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    CORE_COMMITTED = "core_committed"
    AI_HANDOFF_PENDING = "ai_handoff_pending"
    COMPLETED = "completed"
    RETRY_REQUIRED = "retry_required"
    REPAIR_REQUIRED = "repair_required"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ContentAIHandoffState(str, Enum):
    PENDING = "pending"
    BINDING_PUBLISHED = "binding_published"
    Q2_ACTIVATED = "q2_activated"
    COMPLETED = "completed"
    RETRY_REQUIRED = "retry_required"
    REPAIR_REQUIRED = "repair_required"


class AIDerivationState(str, Enum):
    BLOCKED_HANDOFF = "blocked_handoff"
    PENDING = "pending"
    PROCESSING = "processing"
    RETRY_REQUIRED = "retry_required"
    BUDGET_PAUSED = "budget_paused"
    AUTHORIZATION_REQUIRED = "authorization_required"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class PreparedDocument:
    """Immutable private Q0/Q1′ payload.

    It is intentionally generic enough for the future Q0 ingress adapters, but
    R4-A uses it only with deterministic fake archive/delete inputs.  The
    ``input_digest`` covers the full content-bearing payload; its spool file
    additionally has its own byte-level ``payload_sha256`` reference.
    """

    prepared_id: str
    action: ContentMutationAction
    entry: Entry | None
    target_knowledge_id: int | None
    provenance: Mapping[str, str]
    parser_version: str
    input_digest: str

    @classmethod
    def for_archive(
        cls,
        entry: Entry,
        *,
        provenance: Mapping[str, str] | None = None,
        parser_version: str = "r4-q1-v1",
        prepared_id: str | None = None,
    ) -> "PreparedDocument":
        if not isinstance(entry, Entry):
            raise TypeError("archive PreparedDocument 必须包含 Entry")
        identifier = prepared_id or uuid.uuid4().hex
        values = cls._normalized_values(
            identifier,
            ContentMutationAction.ARCHIVE,
            entry,
            None,
            provenance,
            parser_version,
        )
        return cls(*values, input_digest=_sha256(cls._digest_payload(*values)))

    @classmethod
    def for_delete(
        cls,
        knowledge_id: int,
        *,
        provenance: Mapping[str, str] | None = None,
        parser_version: str = "r4-q1-v1",
        prepared_id: str | None = None,
    ) -> "PreparedDocument":
        identifier = prepared_id or uuid.uuid4().hex
        values = cls._normalized_values(
            identifier,
            ContentMutationAction.DELETE,
            None,
            knowledge_id,
            provenance,
            parser_version,
        )
        return cls(*values, input_digest=_sha256(cls._digest_payload(*values)))

    @classmethod
    def _normalized_values(
        cls,
        prepared_id: object,
        action: ContentMutationAction,
        entry: Entry | None,
        target_knowledge_id: int | None,
        provenance: Mapping[str, str] | None,
        parser_version: object,
    ) -> tuple[str, ContentMutationAction, Entry | None, int | None, dict[str, str], str]:
        identifier = _require_id(prepared_id, label="prepared_id")
        if not isinstance(action, ContentMutationAction):
            raise TypeError("PreparedDocument action 无效")
        if not isinstance(parser_version, str) or not parser_version.strip():
            raise ValueError("parser_version 无效")
        if provenance is None:
            normalized_provenance: dict[str, str] = {}
        elif isinstance(provenance, Mapping) and all(
            isinstance(key, str)
            and key
            and isinstance(value, str)
            for key, value in provenance.items()
        ):
            normalized_provenance = dict(provenance)
        else:
            raise ValueError("provenance 必须是字符串键值映射")
        if action is ContentMutationAction.ARCHIVE:
            if not isinstance(entry, Entry) or target_knowledge_id is not None:
                raise ValueError("archive PreparedDocument 必须仅包含 Entry")
        elif action is ContentMutationAction.DELETE:
            if entry is not None:
                raise ValueError("delete PreparedDocument 不得包含 Entry")
            target_knowledge_id = _require_positive_int(
                target_knowledge_id, label="target_knowledge_id"
            )
        elif action is ContentMutationAction.APPLY_AI_PATCH:
            # R4-D will add a typed DerivationPatch payload.  Keep the private
            # transport slot explicit now rather than allowing an untyped body.
            raise ValueError("apply_ai_patch 需要 R4-D 的 DerivationPatch 合同")
        return (
            identifier,
            action,
            entry,
            target_knowledge_id,
            normalized_provenance,
            parser_version,
        )

    @staticmethod
    def _digest_payload(
        prepared_id: str,
        action: ContentMutationAction,
        entry: Entry | None,
        target_knowledge_id: int | None,
        provenance: Mapping[str, str],
        parser_version: str,
    ) -> dict[str, object]:
        # The random identity is deliberately excluded: equivalent source input
        # has one stable content digest even when it was admitted twice.
        del prepared_id
        return {
            "schema_version": _SPOOL_SCHEMA_VERSION,
            "action": action.value,
            "entry": asdict(entry) if entry is not None else None,
            "target_knowledge_id": target_knowledge_id,
            "provenance": dict(provenance),
            "parser_version": parser_version,
        }

    def to_payload(self) -> dict[str, object]:
        values = self._normalized_values(
            self.prepared_id,
            self.action,
            self.entry,
            self.target_knowledge_id,
            self.provenance,
            self.parser_version,
        )
        expected = _sha256(self._digest_payload(*values))
        if self.input_digest != expected:
            raise ValueError("PreparedDocument input_digest 不匹配")
        return {
            "schema_version": _SPOOL_SCHEMA_VERSION,
            "prepared_id": self.prepared_id,
            "action": self.action.value,
            "entry": asdict(self.entry) if self.entry is not None else None,
            "target_knowledge_id": self.target_knowledge_id,
            "provenance": dict(self.provenance),
            "parser_version": self.parser_version,
            "input_digest": self.input_digest,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "PreparedDocument":
        if not isinstance(payload, Mapping):
            raise ValueError("PreparedDocument spool 不是对象")
        if set(payload) != {
            "schema_version",
            "prepared_id",
            "action",
            "entry",
            "target_knowledge_id",
            "provenance",
            "parser_version",
            "input_digest",
        }:
            raise ValueError("PreparedDocument spool 字段无效")
        if payload.get("schema_version") != _SPOOL_SCHEMA_VERSION:
            raise ValueError("PreparedDocument spool schema_version 不受支持")
        try:
            action = ContentMutationAction(payload.get("action"))
        except (TypeError, ValueError) as exc:
            raise ValueError("PreparedDocument action 无效") from exc
        raw_entry = payload.get("entry")
        entry: Entry | None
        if raw_entry is None:
            entry = None
        elif isinstance(raw_entry, Mapping) and set(raw_entry) == _ENTRY_FIELD_NAMES:
            entry = Entry(**dict(raw_entry))
        else:
            raise ValueError("PreparedDocument Entry 无效")
        values = cls._normalized_values(
            payload.get("prepared_id"),
            action,
            entry,
            payload.get("target_knowledge_id"),
            payload.get("provenance"),
            payload.get("parser_version"),
        )
        input_digest = _require_sha256(payload.get("input_digest"), label="input_digest")
        expected = _sha256(cls._digest_payload(*values))
        if input_digest != expected:
            raise ValueError("PreparedDocument input_digest 不匹配")
        return cls(*values, input_digest=input_digest)


@dataclass(frozen=True)
class PreparedDocumentReference:
    prepared_id: str
    payload_sha256: str


class PreparedDocumentSpool:
    """Safe immutable payload files below the private runtime state directory."""

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._root = Path(layout.runtime_state_dir) / "r4" / "prepared"

    def _root_for_write(self) -> Path:
        require_active_data_root_writer(self._layout, owner="r4_prepared_spool")
        return ensure_safe_directory(self._root, label="R4 PreparedDocument spool")

    def _root_for_read(self) -> Path:
        return validate_directory_components(self._root, label="R4 PreparedDocument spool")

    @staticmethod
    def _path(root: Path, prepared_id: str) -> Path:
        return root / f"{_require_id(prepared_id, label='prepared_id')}.json"

    def write(self, document: PreparedDocument) -> PreparedDocumentReference:
        if not isinstance(document, PreparedDocument):
            raise TypeError("document 必须是 PreparedDocument")
        root = self._root_for_write()
        target = self._path(root, document.prepared_id)
        encoded = _canonical_bytes(document.to_payload()) + b"\n"
        digest = hashlib.sha256(encoded).hexdigest()
        if os.path.lexists(target):
            existing = self.read(
                PreparedDocumentReference(document.prepared_id, digest)
            )
            if existing.to_payload() != document.to_payload():
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "同一 PreparedDocument identity 的私有 payload 已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return PreparedDocumentReference(document.prepared_id, digest)
        atomic_publish_file(target, label="R4 PreparedDocument payload", data=encoded)
        return PreparedDocumentReference(document.prepared_id, digest)

    def read(self, reference: PreparedDocumentReference) -> PreparedDocument:
        if not isinstance(reference, PreparedDocumentReference):
            raise TypeError("reference 必须是 PreparedDocumentReference")
        prepared_id = _require_id(reference.prepared_id, label="prepared_id")
        expected_digest = _require_sha256(reference.payload_sha256, label="payload_sha256")
        root = self._root_for_read()
        target = self._path(root, prepared_id)
        try:
            with open_user_file_nofollow(
                target,
                "rb",
                label="R4 PreparedDocument payload",
            ) as handle:
                encoded = handle.read()
        except (OSError, PKVRuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "PreparedDocument payload 无法安全读取。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        if hashlib.sha256(encoded).hexdigest() != expected_digest:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "PreparedDocument payload 摘要不匹配。",
                stage=_STAGE,
                recoverable=True,
            )
        try:
            payload = json.loads(encoded.decode("utf-8"))
            document = PreparedDocument.from_payload(payload)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "PreparedDocument payload 不可验证。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        if document.prepared_id != prepared_id:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "PreparedDocument payload identity 不匹配。",
                stage=_STAGE,
                recoverable=True,
            )
        return document

    def discard(self, reference: PreparedDocumentReference) -> bool:
        """Best-effort removal only after a terminal Q1′ proof.

        A missing, altered, linked, or concurrently replaced spool item is left
        untouched for repair instead of being guessed at.  Callers deliberately
        treat ``False`` as retained recovery evidence: cleanup must never turn a
        proven content commit into a failed or destructive operation.
        """

        if not isinstance(reference, PreparedDocumentReference):
            raise TypeError("reference 必须是 PreparedDocumentReference")
        prepared_id = _require_id(reference.prepared_id, label="prepared_id")
        expected_digest = _require_sha256(reference.payload_sha256, label="payload_sha256")
        target = self._path(self._root_for_write(), prepared_id)
        try:
            before = os.lstat(target)
            if not stat.S_ISREG(before.st_mode):
                return False
            with open_user_file_nofollow(
                target,
                "rb",
                label="R4 PreparedDocument payload",
            ) as handle:
                encoded = handle.read()
            if hashlib.sha256(encoded).hexdigest() != expected_digest:
                return False
            current = os.lstat(target)
            if (
                not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            ):
                return False
            target.unlink()
        except (FileNotFoundError, OSError, PKVRuntimeError):
            return False
        return not os.path.lexists(target)


@dataclass(frozen=True)
class ContentMutationTask:
    task_id: str
    operation_id: str
    action: ContentMutationAction
    prepared_ref: str | None
    prepared_sha256: str | None
    patch_ref: str | None
    patch_sha256: str | None
    target_knowledge_id: int | None
    target_revision_sha256: str | None
    state: ContentMutationState
    attempt_count: int
    claim_token: str | None = None
    claimed_until: str | None = None
    owner_fence: int = 0
    not_before: str | None = None
    last_error_code: str | None = None

    @property
    def prepared_reference(self) -> PreparedDocumentReference | None:
        if self.prepared_ref is None or self.prepared_sha256 is None:
            return None
        return PreparedDocumentReference(self.prepared_ref, self.prepared_sha256)


@dataclass(frozen=True)
class ContentAIHandoff:
    operation_id: str
    derivation_task_id: str
    state: ContentAIHandoffState
    source_digest: str | None
    binding_state: str | None
    last_error_code: str | None


@dataclass(frozen=True)
class AIDerivationTask:
    task_id: str
    operation_id: str
    target_knowledge_id: int | None
    target_revision_sha256: str | None
    source_digest: str | None
    policy_fingerprint: str | None
    patch_ref: str | None
    patch_sha256: str | None
    patch_applied: bool
    state: AIDerivationState
    attempt_count: int
    claim_token: str | None = None
    claimed_until: str | None = None
    owner_fence: int = 0
    not_before: str | None = None
    last_error_code: str | None = None


def _content_task_from_row(row: sqlite3.Row) -> ContentMutationTask:
    try:
        prepared_ref = row["prepared_ref"]
        prepared_sha256 = row["prepared_sha256"]
        patch_ref = row["patch_ref"]
        patch_sha256 = row["patch_sha256"]
        target = row["target_knowledge_id"]
        target_revision = row["target_revision_sha256"]
        attempts = row["attempt_count"]
        owner_fence = row["owner_fence"]
        if type(attempts) is not int or attempts < 0:
            raise ValueError("attempt_count")
        if type(owner_fence) is not int or owner_fence < 0:
            raise ValueError("owner_fence")
        task = ContentMutationTask(
            task_id=_require_id(row["task_id"], label="task_id"),
            operation_id=_require_id(row["operation_id"], label="operation_id"),
            action=ContentMutationAction(row["action"]),
            prepared_ref=(
                _require_id(prepared_ref, label="prepared_ref")
                if prepared_ref is not None
                else None
            ),
            prepared_sha256=(
                _require_sha256(prepared_sha256, label="prepared_sha256")
                if prepared_sha256 is not None
                else None
            ),
            patch_ref=(
                _require_id(patch_ref, label="patch_ref") if patch_ref is not None else None
            ),
            patch_sha256=(
                _require_sha256(patch_sha256, label="patch_sha256")
                if patch_sha256 is not None
                else None
            ),
            target_knowledge_id=(
                _require_positive_int(target, label="target_knowledge_id")
                if target is not None
                else None
            ),
            target_revision_sha256=(
                _require_sha256(target_revision, label="target_revision_sha256")
                if target_revision is not None
                else None
            ),
            state=ContentMutationState(row["state"]),
            attempt_count=attempts,
            claim_token=(
                _require_id(row["claim_token"], label="claim_token")
                if row["claim_token"] is not None
                else None
            ),
            claimed_until=(
                str(row["claimed_until"])
                if row["claimed_until"] is not None
                else None
            ),
            owner_fence=owner_fence,
            not_before=(
                str(row["not_before"])
                if row["not_before"] is not None
                else None
            ),
            last_error_code=_require_error_code(row["last_error_code"]),
        )
        if task.action is ContentMutationAction.ARCHIVE and task.prepared_reference is None:
            raise ValueError("archive task missing payload")
        if task.action is ContentMutationAction.DELETE and task.target_knowledge_id is None:
            raise ValueError("delete task missing target")
        if task.action is ContentMutationAction.APPLY_AI_PATCH and (
            task.patch_ref is None
            or task.patch_sha256 is None
            or task.target_knowledge_id is None
            or task.target_revision_sha256 is None
        ):
            raise ValueError("apply_ai_patch task missing patch/revision")
        return task
    except (KeyError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Q1′ 内容提交任务记录不可验证。",
            stage=_STAGE,
            recoverable=True,
        ) from exc


def _handoff_from_row(row: sqlite3.Row) -> ContentAIHandoff:
    try:
        source = row["source_digest"]
        return ContentAIHandoff(
            operation_id=_require_id(row["operation_id"], label="operation_id"),
            derivation_task_id=_require_id(
                row["derivation_task_id"], label="derivation_task_id"
            ),
            state=ContentAIHandoffState(row["state"]),
            source_digest=(
                _require_sha256(source, label="source_digest") if source is not None else None
            ),
            binding_state=(
                str(row["binding_state"]) if row["binding_state"] is not None else None
            ),
            last_error_code=_require_error_code(row["last_error_code"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Q1′ AI handoff 记录不可验证。",
            stage=_STAGE,
            recoverable=True,
        ) from exc


def _derivation_task_from_row(row: sqlite3.Row) -> AIDerivationTask:
    try:
        source = row["source_digest"]
        policy = row["policy_fingerprint"]
        target = row["target_knowledge_id"]
        target_revision = row["target_revision_sha256"]
        patch_ref = row["patch_ref"]
        patch_sha256 = row["patch_sha256"]
        patch_applied = row["patch_applied"]
        fence = row["owner_fence"]
        attempts = row["attempt_count"]
        if type(attempts) is not int or attempts < 0:
            raise ValueError("attempt_count")
        if type(fence) is not int or fence < 0:
            raise ValueError("owner_fence")
        if patch_applied not in {0, 1}:
            raise ValueError("patch_applied")
        if (patch_ref is None) != (patch_sha256 is None):
            raise ValueError("patch reference incomplete")
        return AIDerivationTask(
            task_id=_require_id(row["task_id"], label="task_id"),
            operation_id=_require_id(row["operation_id"], label="operation_id"),
            target_knowledge_id=(
                _require_positive_int(target, label="target_knowledge_id")
                if target is not None
                else None
            ),
            target_revision_sha256=(
                _require_sha256(target_revision, label="target_revision_sha256")
                if target_revision is not None
                else None
            ),
            source_digest=(
                _require_sha256(source, label="source_digest") if source is not None else None
            ),
            policy_fingerprint=(
                _require_sha256(policy, label="policy_fingerprint")
                if policy is not None
                else None
            ),
            patch_ref=(
                _require_id(patch_ref, label="patch_ref") if patch_ref is not None else None
            ),
            patch_sha256=(
                _require_sha256(patch_sha256, label="patch_sha256")
                if patch_sha256 is not None
                else None
            ),
            patch_applied=bool(patch_applied),
            state=AIDerivationState(row["state"]),
            attempt_count=attempts,
            claim_token=(
                _require_id(row["claim_token"], label="claim_token")
                if row["claim_token"] is not None
                else None
            ),
            claimed_until=(
                str(row["claimed_until"]) if row["claimed_until"] is not None else None
            ),
            owner_fence=fence,
            not_before=(
                str(row["not_before"]) if row["not_before"] is not None else None
            ),
            last_error_code=_require_error_code(row["last_error_code"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Q2 AI 派生任务记录不可验证。",
            stage=_STAGE,
            recoverable=True,
        ) from exc


class ContentLifecycleStore:
    """SQLite state machine for R4 Q1′ and its durable Q2 handoff."""

    _TASK_COLUMNS = """
        task_id, operation_id, action, prepared_ref, prepared_sha256, patch_ref, patch_sha256,
        target_knowledge_id, target_revision_sha256, state, attempt_count, claim_token, claimed_until,
        owner_fence, not_before, last_error_code
    """
    _HANDOFF_COLUMNS = """
        operation_id, derivation_task_id, state, source_digest, binding_state,
        last_error_code
    """
    _DERIVATION_COLUMNS = """
        task_id, operation_id, target_knowledge_id, target_revision_sha256,
        source_digest, policy_fingerprint, patch_ref, patch_sha256, patch_applied, state,
        attempt_count, claim_token, claimed_until, owner_fence, not_before,
        last_error_code
    """

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._db_path = Path(layout.db_path)

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = connect_existing_sqlite(self._db_path, read_only=True)
            connection.row_factory = sqlite3.Row
            try:
                yield connection
            finally:
                connection.close()
        except PKVRuntimeError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "R4 生命周期账本无法以只读方式读取。",
                stage=_STAGE,
                recoverable=True,
            ) from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        require_active_data_root_writer(self._layout, owner="r4_content_lifecycle")
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
                "R4 生命周期账本无法持久化。",
                stage=_STAGE,
                recoverable=True,
            ) from exc

    @staticmethod
    def _load_task(connection: sqlite3.Connection, task_id: str) -> ContentMutationTask:
        row = connection.execute(
            f"SELECT {ContentLifecycleStore._TASK_COLUMNS} "
            "FROM content_mutation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q1′ 内容提交任务不存在或已变化。",
                stage=_STAGE,
                recoverable=True,
            )
        return _content_task_from_row(row)

    @staticmethod
    def _load_task_by_operation(
        connection: sqlite3.Connection, operation_id: str
    ) -> ContentMutationTask | None:
        row = connection.execute(
            f"SELECT {ContentLifecycleStore._TASK_COLUMNS} "
            "FROM content_mutation_tasks WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return _content_task_from_row(row) if row is not None else None

    @staticmethod
    def _load_handoff(
        connection: sqlite3.Connection, operation_id: str
    ) -> ContentAIHandoff | None:
        row = connection.execute(
            f"SELECT {ContentLifecycleStore._HANDOFF_COLUMNS} "
            "FROM content_ai_handoffs WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return _handoff_from_row(row) if row is not None else None

    @staticmethod
    def _load_derivation_by_operation(
        connection: sqlite3.Connection, operation_id: str
    ) -> AIDerivationTask | None:
        row = connection.execute(
            f"SELECT {ContentLifecycleStore._DERIVATION_COLUMNS} "
            "FROM ai_derivation_tasks WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return _derivation_task_from_row(row) if row is not None else None

    @staticmethod
    def _load_derivation(
        connection: sqlite3.Connection, task_id: str
    ) -> AIDerivationTask:
        row = connection.execute(
            f"SELECT {ContentLifecycleStore._DERIVATION_COLUMNS} "
            "FROM ai_derivation_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "Q2 派生任务缺失。",
                stage=_STAGE,
                recoverable=True,
            )
        return _derivation_task_from_row(row)

    def enqueue_prepared(
        self,
        document: PreparedDocument,
        reference: PreparedDocumentReference,
        *,
        operation_id: str | None = None,
    ) -> ContentMutationTask:
        """Persist an accepted Q1′ task and a non-claimable Q2 child once."""

        if not isinstance(document, PreparedDocument) or not isinstance(
            reference, PreparedDocumentReference
        ):
            raise TypeError("document/reference 必须是 PreparedDocument 及其引用")
        if reference.prepared_id != document.prepared_id:
            raise ValueError("PreparedDocument 引用与 payload identity 不匹配")
        reference = PreparedDocumentReference(
            _require_id(reference.prepared_id, label="prepared_ref"),
            _require_sha256(reference.payload_sha256, label="prepared_sha256"),
        )
        op_id = _require_id(operation_id or uuid.uuid4().hex, label="operation_id")
        with self._write_transaction() as connection:
            existing = self._load_task_by_operation(connection, op_id)
            if existing is not None:
                if (
                    existing.action is document.action
                    and existing.prepared_ref == reference.prepared_id
                    and existing.prepared_sha256 == reference.payload_sha256
                    and existing.target_knowledge_id == document.target_knowledge_id
                ):
                    return existing
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "同一 operation_id 的 Q1′ 输入已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            task_id = uuid.uuid4().hex
            derivation_task_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO content_mutation_tasks(
                    task_id, operation_id, action, prepared_ref, prepared_sha256,
                    target_knowledge_id, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    op_id,
                    document.action.value,
                    reference.prepared_id,
                    reference.payload_sha256,
                    document.target_knowledge_id,
                    ContentMutationState.ACCEPTED.value,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_derivation_tasks(task_id, operation_id, state)
                VALUES (?, ?, ?)
                """,
                (derivation_task_id, op_id, AIDerivationState.BLOCKED_HANDOFF.value),
            )
            return self._load_task(connection, task_id)

    def enqueue_ai_patch(
        self,
        *,
        patch_ref: str,
        patch_sha256: str,
        target_knowledge_id: int,
        target_revision_sha256: str,
        operation_id: str | None = None,
    ) -> ContentMutationTask:
        """Queue a Q1′ apply_ai_patch task without recursively creating Q2."""

        patch_ref = _require_id(patch_ref, label="patch_ref")
        patch_sha256 = _require_sha256(patch_sha256, label="patch_sha256")
        target_knowledge_id = _require_positive_int(
            target_knowledge_id, label="target_knowledge_id"
        )
        target_revision_sha256 = _require_sha256(
            target_revision_sha256, label="target_revision_sha256"
        )
        op_id = _require_id(operation_id or uuid.uuid4().hex, label="operation_id")
        with self._write_transaction() as connection:
            existing = self._load_task_by_operation(connection, op_id)
            if existing is not None:
                if (
                    existing.action is ContentMutationAction.APPLY_AI_PATCH
                    and existing.patch_ref == patch_ref
                    and existing.patch_sha256 == patch_sha256
                    and existing.target_knowledge_id == target_knowledge_id
                    and existing.target_revision_sha256 == target_revision_sha256
                ):
                    return existing
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "同一 patch operation_id 的 Q1′ 输入已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            task_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO content_mutation_tasks(
                    task_id, operation_id, action, patch_ref, patch_sha256,
                    target_knowledge_id, target_revision_sha256, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    op_id,
                    ContentMutationAction.APPLY_AI_PATCH.value,
                    patch_ref,
                    patch_sha256,
                    target_knowledge_id,
                    target_revision_sha256,
                    ContentMutationState.ACCEPTED.value,
                ),
            )
            return self._load_task(connection, task_id)

    def get_task(self, task_id: str) -> ContentMutationTask | None:
        task_id = _require_id(task_id, label="task_id")
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {self._TASK_COLUMNS} FROM content_mutation_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return _content_task_from_row(row) if row is not None else None

    def get_task_by_operation(self, operation_id: str) -> ContentMutationTask | None:
        operation_id = _require_id(operation_id, label="operation_id")
        with self._read_connection() as connection:
            return self._load_task_by_operation(connection, operation_id)

    def get_ai_patch_task(self, patch_ref: str) -> ContentMutationTask | None:
        patch_ref = _require_id(patch_ref, label="patch_ref")
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {self._TASK_COLUMNS} FROM content_mutation_tasks "
                "WHERE action = ? AND patch_ref = ? ORDER BY created_at ASC, task_id ASC LIMIT 1",
                (ContentMutationAction.APPLY_AI_PATCH.value, patch_ref),
            ).fetchone()
        return _content_task_from_row(row) if row is not None else None

    def get_handoff(self, operation_id: str) -> ContentAIHandoff | None:
        operation_id = _require_id(operation_id, label="operation_id")
        with self._read_connection() as connection:
            return self._load_handoff(connection, operation_id)

    def get_derivation_task(self, operation_id: str) -> AIDerivationTask | None:
        operation_id = _require_id(operation_id, label="operation_id")
        with self._read_connection() as connection:
            return self._load_derivation_by_operation(connection, operation_id)

    def get_derivation_task_by_id(self, task_id: str) -> AIDerivationTask | None:
        task_id = _require_id(task_id, label="task_id")
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {self._DERIVATION_COLUMNS} FROM ai_derivation_tasks "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return _derivation_task_from_row(row) if row is not None else None

    def list_refreshable_derivations(self) -> tuple[AIDerivationTask, ...]:
        """Return unclaimed Q2 work that may be rechecked on a safe trigger."""

        states = (
            AIDerivationState.PENDING.value,
            AIDerivationState.RETRY_REQUIRED.value,
            AIDerivationState.BUDGET_PAUSED.value,
            AIDerivationState.AUTHORIZATION_REQUIRED.value,
        )
        placeholders = ", ".join("?" for _ in states)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT {self._DERIVATION_COLUMNS} FROM ai_derivation_tasks "
                f"WHERE state IN ({placeholders}) ORDER BY created_at ASC, task_id ASC",
                states,
            ).fetchall()
        return tuple(_derivation_task_from_row(row) for row in rows)

    def list_recoverable_tasks(self) -> tuple[ContentMutationTask, ...]:
        """Return only tasks whose next Q1′ trigger is allowed to inspect them."""

        states = (
            ContentMutationState.ACCEPTED.value,
            ContentMutationState.PROCESSING.value,
            ContentMutationState.RETRY_REQUIRED.value,
            ContentMutationState.CORE_COMMITTED.value,
            ContentMutationState.AI_HANDOFF_PENDING.value,
        )
        placeholders = ", ".join("?" for _ in states)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT {self._TASK_COLUMNS} FROM content_mutation_tasks "
                f"WHERE state IN ({placeholders}) ORDER BY created_at ASC, task_id ASC",
                states,
            ).fetchall()
        return tuple(_content_task_from_row(row) for row in rows)

    def list_completed_prepared_tasks(self) -> tuple[ContentMutationTask, ...]:
        """Return terminal Q1′ rows whose private body may be conservatively swept."""

        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT {self._TASK_COLUMNS} FROM content_mutation_tasks "
                "WHERE state = ? AND prepared_ref IS NOT NULL AND prepared_sha256 IS NOT NULL "
                "ORDER BY updated_at ASC, task_id ASC",
                (ContentMutationState.COMPLETED.value,),
            ).fetchall()
        return tuple(_content_task_from_row(row) for row in rows)

    def claim_next(self, *, lease_seconds: int = 120) -> ContentMutationTask | None:
        """Claim one safe-to-run Q1′ task under the already-held root lease."""

        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须是 1..3600 的整数")
        modifier = f"+{lease_seconds} seconds"
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT task_id FROM content_mutation_tasks
                WHERE state IN (?, ?)
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                ORDER BY created_at ASC, task_id ASC
                LIMIT 1
                """,
                (
                    ContentMutationState.ACCEPTED.value,
                    ContentMutationState.RETRY_REQUIRED.value,
                ),
            ).fetchone()
            if row is None:
                return None
            task_id = _require_id(row["task_id"], label="task_id")
            token = uuid.uuid4().hex
            changed = connection.execute(
                """
                UPDATE content_mutation_tasks
                SET state = ?, claim_token = ?, claimed_until = datetime('now', ?),
                    owner_fence = owner_fence + 1,
                    attempt_count = attempt_count + 1, last_error_code = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN (?, ?)
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                """,
                (
                    ContentMutationState.PROCESSING.value,
                    token,
                    modifier,
                    task_id,
                    ContentMutationState.ACCEPTED.value,
                    ContentMutationState.RETRY_REQUIRED.value,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ 任务在 claim 前已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_task(connection, task_id)

    def recover_processing_task(self, task_id: str, *, error_code: str = "worker_restarted") -> ContentMutationTask:
        """Release a stale Q1′ processing claim after a restart trigger.

        R4-A has no daemon and the caller already owns the root lease, so no
        live Q1′ worker can be safely concurrent with this recovery.  The
        operation journal is checked by the application before this transition;
        a proven core commit is advanced instead of retried.
        """

        task_id = _require_id(task_id, label="task_id")
        error_code = _require_error_code(error_code)
        assert error_code is not None
        with self._write_transaction() as connection:
            task = self._load_task(connection, task_id)
            if task.state is not ContentMutationState.PROCESSING:
                return task
            connection.execute(
                """
                UPDATE content_mutation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    last_error_code = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ?
                """,
                (
                    ContentMutationState.RETRY_REQUIRED.value,
                    error_code,
                    task_id,
                    ContentMutationState.PROCESSING.value,
                ),
            )
            return self._load_task(connection, task_id)

    def mark_rejected(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        error_code: str,
    ) -> ContentMutationTask:
        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            owner_fence=owner_fence,
            state=ContentMutationState.REJECTED,
            error_code=error_code,
        )

    def mark_retry_required(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        error_code: str,
    ) -> ContentMutationTask:
        return self._finish_claim(
            task_id,
            claim_token=claim_token,
            owner_fence=owner_fence,
            state=ContentMutationState.RETRY_REQUIRED,
            error_code=error_code,
        )

    def _finish_claim(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        state: ContentMutationState,
        error_code: str,
    ) -> ContentMutationTask:
        task_id = _require_id(task_id, label="task_id")
        token = _require_id(claim_token, label="claim_token")
        if type(owner_fence) is not int or owner_fence <= 0:
            raise ValueError("owner_fence 必须是正整数")
        code = _require_error_code(error_code)
        assert code is not None
        with self._write_transaction() as connection:
            changed = connection.execute(
                """
                UPDATE content_mutation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    last_error_code = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    state.value,
                    code,
                    task_id,
                    ContentMutationState.PROCESSING.value,
                    token,
                    owner_fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ 任务 claim 已失效。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_task(connection, task_id)

    def record_core_committed(
        self,
        task_id: str,
        *,
        operation_id: str,
        knowledge_id: int | None = None,
        target_revision_sha256: str | None = None,
        claim_token: str | None = None,
        owner_fence: int | None = None,
    ) -> ContentMutationTask:
        """Record the proven StorageCoordinator commit in its own durable step."""

        task_id = _require_id(task_id, label="task_id")
        operation_id = _require_id(operation_id, label="operation_id")
        if knowledge_id is not None:
            knowledge_id = _require_positive_int(knowledge_id, label="knowledge_id")
        if target_revision_sha256 is not None:
            target_revision_sha256 = _require_sha256(
                target_revision_sha256,
                label="target_revision_sha256",
            )
        if (claim_token is None) != (owner_fence is None):
            raise ValueError("claim_token 与 owner_fence 必须同时提供")
        if claim_token is not None:
            claim_token = _require_id(claim_token, label="claim_token")
            if type(owner_fence) is not int or owner_fence <= 0:
                raise ValueError("owner_fence 必须是正整数")
        with self._write_transaction() as connection:
            task = self._load_task(connection, task_id)
            if task.operation_id != operation_id:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Storage operation 与 Q1′ 任务 identity 不匹配。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if task.state in {
                ContentMutationState.CORE_COMMITTED,
                ContentMutationState.AI_HANDOFF_PENDING,
                ContentMutationState.COMPLETED,
            }:
                if (
                    knowledge_id is not None
                    and task.target_knowledge_id is not None
                    and task.target_knowledge_id != knowledge_id
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "Q1′ task 的已提交知识 identity 不一致。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                if (
                    (knowledge_id is not None and task.target_knowledge_id is None)
                    or (
                        target_revision_sha256 is not None
                        and task.target_revision_sha256 is None
                    )
                ):
                    connection.execute(
                        """
                        UPDATE content_mutation_tasks
                        SET target_knowledge_id = COALESCE(?, target_knowledge_id),
                            target_revision_sha256 = COALESCE(?, target_revision_sha256),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE task_id = ?
                        """,
                        (knowledge_id, target_revision_sha256, task_id),
                    )
                    return self._load_task(connection, task_id)
                if (
                    target_revision_sha256 is not None
                    and task.target_revision_sha256 is not None
                    and task.target_revision_sha256 != target_revision_sha256
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "Q1′ task 的已提交 revision 不一致。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                return task
            if task.state is not ContentMutationState.PROCESSING:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ 任务不能在当前状态登记 core commit。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if claim_token is None or owner_fence is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ core commit 需要当前 fenced claim。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if task.claim_token != claim_token or task.owner_fence != owner_fence:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ core commit claim 已失效。",
                    stage=_STAGE,
                    recoverable=True,
                )
            changed = connection.execute(
                """
                UPDATE content_mutation_tasks
                SET state = ?, target_knowledge_id = COALESCE(?, target_knowledge_id),
                    target_revision_sha256 = COALESCE(?, target_revision_sha256),
                    claim_token = NULL, claimed_until = NULL, last_error_code = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    ContentMutationState.CORE_COMMITTED.value,
                    knowledge_id,
                    target_revision_sha256,
                    task_id,
                    ContentMutationState.PROCESSING.value,
                    claim_token,
                    owner_fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ core commit 写入已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_task(connection, task_id)

    def ensure_handoff(self, operation_id: str) -> ContentAIHandoff:
        """Create the durable outbox record once after a proven core commit."""

        operation_id = _require_id(operation_id, label="operation_id")
        with self._write_transaction() as connection:
            task = self._load_task_by_operation(connection, operation_id)
            if task is None:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "Storage operation 没有对应的 Q1′ 内容任务。",
                    stage=_STAGE,
                    recoverable=True,
                )
            existing = self._load_handoff(connection, operation_id)
            if existing is not None:
                return existing
            if task.state not in {
                ContentMutationState.CORE_COMMITTED,
                ContentMutationState.AI_HANDOFF_PENDING,
                ContentMutationState.COMPLETED,
            }:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "未证明核心提交，不能创建 AI handoff。",
                    stage=_STAGE,
                    recoverable=True,
                )
            derivation = self._load_derivation_by_operation(connection, operation_id)
            if derivation is None or derivation.state is not AIDerivationState.BLOCKED_HANDOFF:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "Q1′ handoff 缺少受阻塞的 Q2 任务。",
                    stage=_STAGE,
                    recoverable=True,
                )
            connection.execute(
                """
                INSERT INTO content_ai_handoffs(operation_id, derivation_task_id, state)
                VALUES (?, ?, ?)
                """,
                (
                    operation_id,
                    derivation.task_id,
                    ContentAIHandoffState.PENDING.value,
                ),
            )
            if task.state is ContentMutationState.CORE_COMMITTED:
                connection.execute(
                    """
                    UPDATE content_mutation_tasks
                    SET state = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = ? AND state = ?
                    """,
                    (
                        ContentMutationState.AI_HANDOFF_PENDING.value,
                        task.task_id,
                        ContentMutationState.CORE_COMMITTED.value,
                    ),
                )
            handoff = self._load_handoff(connection, operation_id)
            assert handoff is not None
            return handoff

    def mark_binding_published(
        self,
        operation_id: str,
        *,
        source_digest: str,
        binding_state: str,
    ) -> ContentAIHandoff:
        operation_id = _require_id(operation_id, label="operation_id")
        source_digest = _require_sha256(source_digest, label="source_digest")
        if not isinstance(binding_state, str) or not binding_state:
            raise ValueError("binding_state 无效")
        with self._write_transaction() as connection:
            handoff = self._load_handoff(connection, operation_id)
            if handoff is None:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "不能为缺失 handoff 发布 binding。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if handoff.state in {
                ContentAIHandoffState.BINDING_PUBLISHED,
                ContentAIHandoffState.Q2_ACTIVATED,
                ContentAIHandoffState.COMPLETED,
            }:
                if (
                    handoff.source_digest != source_digest
                    or handoff.binding_state != binding_state
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "已发布的 Embedding binding 与 handoff 不一致。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                return handoff
            connection.execute(
                """
                UPDATE content_ai_handoffs
                SET state = ?, source_digest = ?, binding_state = ?,
                    last_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ? AND state IN (?, ?)
                """,
                (
                    ContentAIHandoffState.BINDING_PUBLISHED.value,
                    source_digest,
                    binding_state,
                    operation_id,
                    ContentAIHandoffState.PENDING.value,
                    ContentAIHandoffState.RETRY_REQUIRED.value,
                ),
            )
            updated = self._load_handoff(connection, operation_id)
            assert updated is not None
            return updated

    def mark_handoff_retry(
        self,
        operation_id: str,
        *,
        error_code: str,
    ) -> ContentAIHandoff:
        operation_id = _require_id(operation_id, label="operation_id")
        error_code = _require_error_code(error_code)
        assert error_code is not None
        with self._write_transaction() as connection:
            handoff = self._load_handoff(connection, operation_id)
            if handoff is None:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "不能更新缺失的 Q1′ handoff。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if handoff.state in {
                ContentAIHandoffState.Q2_ACTIVATED,
                ContentAIHandoffState.COMPLETED,
            }:
                return handoff
            connection.execute(
                """
                UPDATE content_ai_handoffs
                SET state = ?, last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ?
                """,
                (
                    ContentAIHandoffState.RETRY_REQUIRED.value,
                    error_code,
                    operation_id,
                ),
            )
            updated = self._load_handoff(connection, operation_id)
            assert updated is not None
            return updated

    def activate_derivation(
        self,
        operation_id: str,
        *,
        source_digest: str,
        policy_fingerprint: str | None,
        state: AIDerivationState,
    ) -> AIDerivationTask:
        """Activate Q2 only after the durable handoff/binding publication."""

        operation_id = _require_id(operation_id, label="operation_id")
        source_digest = _require_sha256(source_digest, label="source_digest")
        if policy_fingerprint is not None:
            policy_fingerprint = _require_sha256(
                policy_fingerprint, label="policy_fingerprint"
            )
        if state not in {
            AIDerivationState.PENDING,
            AIDerivationState.AUTHORIZATION_REQUIRED,
            AIDerivationState.BUDGET_PAUSED,
        }:
            raise ValueError("Q2 activation state 无效")
        with self._write_transaction() as connection:
            handoff = self._load_handoff(connection, operation_id)
            if handoff is None or handoff.state not in {
                ContentAIHandoffState.BINDING_PUBLISHED,
                ContentAIHandoffState.Q2_ACTIVATED,
                ContentAIHandoffState.COMPLETED,
            }:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q1′ handoff 尚未完成 binding 发布；Q2 不可激活。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if handoff.source_digest != source_digest:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 source 与 durable handoff 不一致。",
                    stage=_STAGE,
                    recoverable=True,
                )
            derivation = self._load_derivation(connection, handoff.derivation_task_id)
            mutation = self._load_task_by_operation(connection, operation_id)
            if (
                mutation is None
                or mutation.target_knowledge_id is None
                or mutation.target_revision_sha256 is None
            ):
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "Q2 activation 缺少 Q1′ 目标 revision 证明。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if derivation.state is not AIDerivationState.BLOCKED_HANDOFF:
                if (
                    derivation.target_knowledge_id != mutation.target_knowledge_id
                    or derivation.target_revision_sha256
                    != mutation.target_revision_sha256
                    or derivation.source_digest != source_digest
                    or derivation.policy_fingerprint != policy_fingerprint
                    or derivation.state is not state
                ):
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "已激活的 Q2 任务与当前 handoff 不一致。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                return derivation
            connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET target_knowledge_id = ?, target_revision_sha256 = ?,
                    source_digest = ?, policy_fingerprint = ?, state = ?,
                    claim_token = NULL, claimed_until = NULL, last_error_code = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ?
                """,
                (
                    mutation.target_knowledge_id,
                    mutation.target_revision_sha256,
                    source_digest,
                    policy_fingerprint,
                    state.value,
                    derivation.task_id,
                    AIDerivationState.BLOCKED_HANDOFF.value,
                ),
            )
            connection.execute(
                """
                UPDATE content_ai_handoffs
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ? AND state = ?
                """,
                (
                    ContentAIHandoffState.Q2_ACTIVATED.value,
                    operation_id,
                    ContentAIHandoffState.BINDING_PUBLISHED.value,
                ),
            )
            connection.execute(
                """
                UPDATE content_mutation_tasks
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE operation_id = ? AND state = ?
                """,
                (
                    ContentMutationState.COMPLETED.value,
                    operation_id,
                    ContentMutationState.AI_HANDOFF_PENDING.value,
                ),
            )
            return self._load_derivation(connection, derivation.task_id)

    def recover_expired_derivation_claims(self) -> int:
        """Release only expired Q2 work; a live fenced claim remains untouched."""

        with self._write_transaction() as connection:
            return connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE state = ?
                  AND (claimed_until IS NULL OR claimed_until <= CURRENT_TIMESTAMP)
                """,
                (
                    AIDerivationState.RETRY_REQUIRED.value,
                    "worker_restarted",
                    AIDerivationState.PROCESSING.value,
                ),
            ).rowcount

    def supersede_derivation(
        self,
        task_id: str,
        *,
        error_code: str,
    ) -> AIDerivationTask:
        """Retire unclaimed stale Q2 work without granting it another attempt."""

        task_id = _require_id(task_id, label="task_id")
        error_code = _require_error_code(error_code)
        assert error_code is not None
        with self._write_transaction() as connection:
            task = self._load_derivation(connection, task_id)
            if task.state is AIDerivationState.SUPERSEDED:
                return task
            if task.state is AIDerivationState.PROCESSING:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "不能 supersede 正在被 owner 持有的 Q2 task。",
                    stage=_STAGE,
                    recoverable=True,
                )
            connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    not_before = NULL, last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (AIDerivationState.SUPERSEDED.value, error_code, task_id),
            )
            return self._load_derivation(connection, task_id)

    def supersede_derivations_for_target(
        self,
        target_knowledge_id: int,
        *,
        excluding_operation_id: str,
        error_code: str = "target_deleted",
    ) -> int:
        """Fence every older derivation for a deleted target before Q2 can patch it."""

        target_knowledge_id = _require_positive_int(
            target_knowledge_id, label="target_knowledge_id"
        )
        excluding_operation_id = _require_id(
            excluding_operation_id, label="excluding_operation_id"
        )
        error_code = _require_error_code(error_code)
        assert error_code is not None
        with self._write_transaction() as connection:
            return connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    owner_fence = owner_fence + 1, not_before = NULL,
                    last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE target_knowledge_id = ? AND operation_id <> ?
                  AND state NOT IN (?, ?)
                """,
                (
                    AIDerivationState.SUPERSEDED.value,
                    error_code,
                    target_knowledge_id,
                    excluding_operation_id,
                    AIDerivationState.COMPLETED.value,
                    AIDerivationState.SUPERSEDED.value,
                ),
            ).rowcount

    def mark_retry_exhausted(self, *, max_attempts: int) -> int:
        """Leave an explicit retry_required ceiling before any Provider is built."""

        if type(max_attempts) is not int or not 0 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 0..10 的整数")
        with self._write_transaction() as connection:
            return connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    not_before = NULL, last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE state IN (?, ?) AND attempt_count >= ?
                """,
                (
                    AIDerivationState.RETRY_REQUIRED.value,
                    "retry_exhausted",
                    AIDerivationState.PENDING.value,
                    AIDerivationState.RETRY_REQUIRED.value,
                    max_attempts,
                ),
            ).rowcount

    def claim_next_derivation(
        self,
        *,
        max_attempts: int,
        lease_seconds: int = 120,
    ) -> AIDerivationTask | None:
        """Claim one eligible Q2 task with a non-reusable token and fence."""

        if type(max_attempts) is not int or not 0 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须是 0..10 的整数")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须是 1..3600 的整数")
        modifier = f"+{lease_seconds} seconds"
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT task_id FROM ai_derivation_tasks
                WHERE state IN (?, ?)
                  AND attempt_count < ?
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                ORDER BY created_at ASC, task_id ASC
                LIMIT 1
                """,
                (
                    AIDerivationState.PENDING.value,
                    AIDerivationState.RETRY_REQUIRED.value,
                    max_attempts,
                ),
            ).fetchone()
            if row is None:
                return None
            task_id = _require_id(row["task_id"], label="task_id")
            token = uuid.uuid4().hex
            changed = connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET state = ?, claim_token = ?, claimed_until = datetime('now', ?),
                    owner_fence = owner_fence + 1,
                    attempt_count = attempt_count + 1,
                    last_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN (?, ?)
                  AND attempt_count < ?
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                """,
                (
                    AIDerivationState.PROCESSING.value,
                    token,
                    modifier,
                    task_id,
                    AIDerivationState.PENDING.value,
                    AIDerivationState.RETRY_REQUIRED.value,
                    max_attempts,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 task 在 claim 前已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_derivation(connection, task_id)

    @staticmethod
    def _validate_derivation_claim(
        task: AIDerivationTask,
        claim_token: str,
        owner_fence: int,
    ) -> tuple[str, int]:
        token = _require_id(claim_token, label="claim_token")
        if type(owner_fence) is not int or owner_fence <= 0:
            raise ValueError("owner_fence 必须是正整数")
        if task.claim_token != token or task.owner_fence != owner_fence:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q2 task claim 已失效。",
                stage=_STAGE,
                recoverable=True,
            )
        return token, owner_fence

    def assert_live_derivation_claim(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
    ) -> AIDerivationTask:
        """Fence a local Q2 publication immediately before irreversible writes.

        Provider work is intentionally performed outside the root lease.  Before
        a prefetched result can build/publish a generation, re-read its durable
        claim under that lease and require that it has not expired or been
        superseded by a later worker.
        """

        task_id = _require_id(task_id, label="task_id")
        with self._write_transaction() as connection:
            task = self._load_derivation(connection, task_id)
            token, fence = self._validate_derivation_claim(task, claim_token, owner_fence)
            live = connection.execute(
                """
                SELECT 1 FROM ai_derivation_tasks
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    task_id,
                    AIDerivationState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).fetchone()
            if live is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 task claim 已在 generation 发布前过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return task

    def mark_derivation_state(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        state: AIDerivationState,
        error_code: str | None = None,
        delay_seconds: int = 0,
    ) -> AIDerivationTask:
        """Finish or pause the current Q2 claim with its exact owner fence."""

        if state not in {
            AIDerivationState.PENDING,
            AIDerivationState.RETRY_REQUIRED,
            AIDerivationState.BUDGET_PAUSED,
            AIDerivationState.AUTHORIZATION_REQUIRED,
            AIDerivationState.COMPLETED,
            AIDerivationState.SUPERSEDED,
        }:
            raise ValueError("Q2 terminal/queued state 无效")
        task_id = _require_id(task_id, label="task_id")
        code = _require_error_code(error_code)
        if type(delay_seconds) is not int or not 0 <= delay_seconds <= 86_400:
            raise ValueError("delay_seconds 必须是 0..86400 的整数")
        modifier = f"+{delay_seconds} seconds"
        with self._write_transaction() as connection:
            task = self._load_derivation(connection, task_id)
            token, fence = self._validate_derivation_claim(task, claim_token, owner_fence)
            changed = connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    not_before = CASE WHEN ? THEN datetime('now', ?) ELSE NULL END,
                    last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    state.value,
                    state is AIDerivationState.RETRY_REQUIRED,
                    modifier,
                    code,
                    task_id,
                    AIDerivationState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 task 状态写入已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_derivation(connection, task_id)

    def replan_derivation(
        self,
        task_id: str,
        *,
        source_digest: str,
        policy_fingerprint: str | None,
        state: AIDerivationState,
    ) -> AIDerivationTask:
        """Requeue a stale, unclaimed Q2 task under the current source/policy."""

        task_id = _require_id(task_id, label="task_id")
        source_digest = _require_sha256(source_digest, label="source_digest")
        if policy_fingerprint is not None:
            policy_fingerprint = _require_sha256(
                policy_fingerprint, label="policy_fingerprint"
            )
        if state not in {
            AIDerivationState.PENDING,
            AIDerivationState.AUTHORIZATION_REQUIRED,
            AIDerivationState.BUDGET_PAUSED,
        }:
            raise ValueError("Q2 replan state 无效")
        with self._write_transaction() as connection:
            task = self._load_derivation(connection, task_id)
            if task.state is AIDerivationState.PROCESSING:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "不能重规划仍被其他 owner 持有的 Q2 task。",
                    stage=_STAGE,
                    recoverable=True,
                )
            connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET source_digest = ?, policy_fingerprint = ?, state = ?,
                    claim_token = NULL, claimed_until = NULL, not_before = NULL,
                    last_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
                """,
                (source_digest, policy_fingerprint, state.value, task_id),
            )
            return self._load_derivation(connection, task_id)

    def record_derivation_patch(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        patch_ref: str,
        patch_sha256: str,
    ) -> AIDerivationTask:
        """Persist normalized Provider output before Q1′ is allowed to apply it."""

        task_id = _require_id(task_id, label="task_id")
        patch_ref = _require_id(patch_ref, label="patch_ref")
        patch_sha256 = _require_sha256(patch_sha256, label="patch_sha256")
        with self._write_transaction() as connection:
            task = self._load_derivation(connection, task_id)
            token, fence = self._validate_derivation_claim(task, claim_token, owner_fence)
            if task.patch_ref is not None:
                if task.patch_ref != patch_ref or task.patch_sha256 != patch_sha256:
                    raise PKVRuntimeError(
                        ErrorCode.RUNTIME_PLAN_STALE,
                        "Q2 task 已记录不同的 DerivationPatch。",
                        stage=_STAGE,
                        recoverable=True,
                    )
                return task
            changed = connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET patch_ref = ?, patch_sha256 = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    patch_ref,
                    patch_sha256,
                    task_id,
                    AIDerivationState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 DerivationPatch 记录已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_derivation(connection, task_id)

    def mark_derivation_patch_applied(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        source_digest: str,
    ) -> AIDerivationTask:
        task_id = _require_id(task_id, label="task_id")
        source_digest = _require_sha256(source_digest, label="source_digest")
        with self._write_transaction() as connection:
            task = self._load_derivation(connection, task_id)
            token, fence = self._validate_derivation_claim(task, claim_token, owner_fence)
            if task.patch_applied:
                return task
            if task.patch_ref is None or task.patch_sha256 is None:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "不能确认缺失的 DerivationPatch。",
                    stage=_STAGE,
                    recoverable=True,
                )
            changed = connection.execute(
                """
                UPDATE ai_derivation_tasks
                SET patch_applied = 1, source_digest = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    source_digest,
                    task_id,
                    AIDerivationState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q2 DerivationPatch 确认已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load_derivation(connection, task_id)

    def complete_patch_task(self, task_id: str) -> ContentMutationTask:
        """Finish a proven Q1′ apply_ai_patch task without another Q2 handoff."""

        task_id = _require_id(task_id, label="task_id")
        with self._write_transaction() as connection:
            task = self._load_task(connection, task_id)
            if task.action is not ContentMutationAction.APPLY_AI_PATCH:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "只有 apply_ai_patch 可走无递归 Q1′ 完成路径。",
                    stage=_STAGE,
                    recoverable=True,
                )
            if task.state is ContentMutationState.COMPLETED:
                return task
            if task.state is not ContentMutationState.CORE_COMMITTED:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "apply_ai_patch 尚未证明核心提交。",
                    stage=_STAGE,
                    recoverable=True,
                )
            connection.execute(
                """
                UPDATE content_mutation_tasks
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ?
                """,
                (
                    ContentMutationState.COMPLETED.value,
                    task_id,
                    ContentMutationState.CORE_COMMITTED.value,
                ),
            )
            return self._load_task(connection, task_id)


__all__ = [
    "AIDerivationState",
    "AIDerivationTask",
    "ContentAIHandoff",
    "ContentAIHandoffState",
    "ContentLifecycleStore",
    "ContentMutationAction",
    "ContentMutationState",
    "ContentMutationTask",
    "PreparedDocument",
    "PreparedDocumentReference",
    "PreparedDocumentSpool",
]
