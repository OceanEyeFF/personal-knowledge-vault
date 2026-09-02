"""Cross-store archive/delete state machine with durable repair records."""

from __future__ import annotations

import json
import os
import stat
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from src.runtime.errors import ErrorCode, OperationStatus, PKVRuntimeError, StorageStage
from src.runtime.layout import (
    atomic_publish_file,
    ensure_safe_directory,
    open_user_file_nofollow,
    validate_directory_components,
)
from src.storage.derivation_patch import DerivationPatch
from src.storage.markdown_store import Entry, MarkdownStore, PlannedVaultWrite
from src.storage.sqlite_store import (
    SQLiteStore,
    entry_projection_sha256,
    row_projection_sha256,
)
from src.storage.vault_paths import QuarantinedVaultFile
from src.utils.text_utils import split_text_into_chunks


VectorOperation = Callable[[int], Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error(code: ErrorCode, message: str, *, recoverable: bool) -> dict[str, Any]:
    return {
        "code": code.value,
        "message": message,
        "recoverable": recoverable,
    }


@dataclass(frozen=True)
class _CommitProbe:
    """Result of reconciling an exception with a transaction-bound proof."""

    state: str
    knowledge_id: Optional[int] = None
    reason: str = ""


def _valid_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _operation_id_or_new(value: str | None) -> str:
    """Return a caller-supplied durable operation identity or create one.

    Q1′ owns the handoff identity before it enters the cross-store coordinator,
    so a restart can join its task row to the existing operation journal.  The
    historical public coordinator callers still omit it and retain the original
    UUID allocation behavior.
    """

    if value is None:
        return uuid.uuid4().hex
    if (
        not isinstance(value, str)
        or not value
        or any(character not in "0123456789abcdef-" for character in value)
    ):
        raise ValueError("operation_id 非法")
    return value


def _proof_matches(
    proof: dict[str, Any],
    *,
    operation_id: str,
    action: str,
    relative_file_path: str,
    projection_sha256: str,
    knowledge_id: int | None = None,
) -> bool:
    proof_id = proof.get("knowledge_id")
    return (
        proof.get("operation_id") == operation_id
        and proof.get("action") == action
        and _valid_positive_int(proof_id)
        and (knowledge_id is None or proof_id == knowledge_id)
        and proof.get("relative_file_path") == relative_file_path
        and proof.get("projection_sha256") == projection_sha256
    )


def _observed_projection_matches(
    sqlite_store: SQLiteStore,
    row: dict[str, Any],
    projection_sha256: str,
) -> bool:
    knowledge_id = row.get("knowledge_id")
    if not _valid_positive_int(knowledge_id):
        return False
    chunks = sqlite_store.get_chunks_by_knowledge_id(knowledge_id)
    return row_projection_sha256(row, chunks) == projection_sha256


def _probe_archive_commit(
    sqlite_store: SQLiteStore,
    *,
    operation_id: str,
    relative_file_path: str,
    projection_sha256: str,
) -> _CommitProbe:
    """Prove archive commit, prove absence, or return an ambiguity."""

    try:
        proof = sqlite_store.query_storage_operation(operation_id)
        if proof is None:
            conflict = sqlite_store.query_by_file_path(relative_file_path)
            if conflict is None:
                return _CommitProbe("absent")
            return _CommitProbe(
                "ambiguous",
                reason="提交凭据缺失但 file_path 已存在 SQLite 记录",
            )
        if not _proof_matches(
            proof,
            operation_id=operation_id,
            action="archive",
            relative_file_path=relative_file_path,
            projection_sha256=projection_sha256,
        ):
            return _CommitProbe("ambiguous", reason="archive 提交凭据字段不匹配")
        knowledge_id = int(proof["knowledge_id"])
        row = sqlite_store.query_by_id(knowledge_id)
        if (
            row is None
            or row.get("file_path") != relative_file_path
            or not _observed_projection_matches(sqlite_store, row, projection_sha256)
        ):
            return _CommitProbe("ambiguous", reason="archive 提交凭据与核心投影不一致")
        return _CommitProbe("committed", knowledge_id=knowledge_id)
    except Exception as exc:
        return _CommitProbe("ambiguous", reason=f"archive 提交状态查询失败: {exc}")


def _probe_delete_commit(
    sqlite_store: SQLiteStore,
    *,
    operation_id: str,
    knowledge_id: int,
    relative_file_path: str,
    projection_sha256: str,
) -> _CommitProbe:
    """Prove delete commit or prove the original SQLite projection remains."""

    try:
        proof = sqlite_store.query_storage_operation(operation_id)
        row = sqlite_store.query_by_id(knowledge_id)
        if proof is not None:
            if not _proof_matches(
                proof,
                operation_id=operation_id,
                action="delete",
                knowledge_id=knowledge_id,
                relative_file_path=relative_file_path,
                projection_sha256=projection_sha256,
            ):
                return _CommitProbe("ambiguous", reason="delete 提交凭据字段不匹配")
            if row is not None:
                return _CommitProbe("ambiguous", reason="delete 提交凭据存在但知识条目仍存在")
            return _CommitProbe("committed", knowledge_id=knowledge_id)
        if row is None:
            return _CommitProbe(
                "ambiguous",
                reason="delete 提交凭据与知识条目均缺失，无法归因删除者",
            )
        if (
            row.get("file_path") != relative_file_path
            or not _observed_projection_matches(sqlite_store, row, projection_sha256)
        ):
            return _CommitProbe("ambiguous", reason="delete 前 SQLite 投影已变化")
        return _CommitProbe("absent", knowledge_id=knowledge_id)
    except Exception as exc:
        return _CommitProbe("ambiguous", reason=f"delete 提交状态查询失败: {exc}")


def _probe_patch_commit(
    sqlite_store: SQLiteStore,
    *,
    operation_id: str,
    knowledge_id: int,
    relative_file_path: str,
    previous_revision_sha256: str,
    resulting_revision_sha256: str,
) -> _CommitProbe:
    """Prove a revision-bound patch commit, its untouched predecessor, or ambiguity."""

    try:
        proof = sqlite_store.query_r4_content_operation(operation_id)
        row = sqlite_store.query_by_id(knowledge_id)
        if proof is not None:
            expected = {
                "operation_id": operation_id,
                "action": "apply_ai_patch",
                "knowledge_id": knowledge_id,
                "relative_file_path": relative_file_path,
                "previous_revision_sha256": previous_revision_sha256,
                "resulting_revision_sha256": resulting_revision_sha256,
            }
            # ``committed_at`` is source-owned; all remaining proof fields bind
            # this recovery request exactly.
            if {key: proof.get(key) for key in expected} != expected:
                return _CommitProbe("ambiguous", reason="patch 提交凭据字段不匹配")
            if (
                row is None
                or row.get("file_path") != relative_file_path
                or not _observed_projection_matches(
                    sqlite_store, row, resulting_revision_sha256
                )
            ):
                return _CommitProbe("ambiguous", reason="patch 凭据与 SQLite 投影不一致")
            return _CommitProbe("committed", knowledge_id=knowledge_id)
        if (
            row is None
            or row.get("file_path") != relative_file_path
            or not _observed_projection_matches(
                sqlite_store, row, previous_revision_sha256
            )
        ):
            return _CommitProbe("ambiguous", reason="patch 前 SQLite 投影已变化")
        return _CommitProbe("absent", knowledge_id=knowledge_id)
    except Exception as exc:
        return _CommitProbe("ambiguous", reason=f"patch 提交状态查询失败: {exc}")


@dataclass(frozen=True)
class StorageOperationResult:
    """Stable result envelope shared by Workflow, wrappers, CLI and MCP adapters."""

    operation_id: str
    action: str
    status: OperationStatus
    stage: StorageStage
    knowledge_id: Optional[int] = None
    file_path: Optional[str] = None
    relative_file_path: Optional[str] = None
    original_path: Optional[str] = None
    quarantine_path: Optional[str] = None
    errors: tuple[dict[str, Any], ...] = ()
    repair_actions: tuple[str, ...] = ()
    core_committed: bool = False

    @property
    def successful(self) -> bool:
        return self.status in {
            OperationStatus.READY,
            OperationStatus.DEGRADED,
            OperationStatus.DELETED,
        }

    @property
    def retry_safe(self) -> bool:
        """True only when nothing was committed and a plain retry cannot duplicate state."""
        return self.status is OperationStatus.REJECTED and not self.core_committed

    @property
    def do_not_retry(self) -> bool:
        """Explicit warning: committed or repair-needing operations must not be blindly retried."""
        return not self.retry_safe

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["stage"] = self.stage.value
        payload["errors"] = list(self.errors)
        payload["repair_actions"] = list(self.repair_actions)
        payload["successful"] = self.successful
        payload["core_committed"] = self.core_committed
        payload["retry_safe"] = self.retry_safe
        payload["do_not_retry"] = self.do_not_retry
        return payload


class StorageOperationJournal:
    """One atomic JSON record per cross-store operation."""

    def __init__(self, journal_dir: Path) -> None:
        self.journal_dir = Path(journal_dir).absolute()
        ensure_safe_directory(self.journal_dir, label="operation journal 目录")
        self._validate_root()

    def _validate_root(self) -> None:
        validate_directory_components(
            self.journal_dir, label="operation journal 目录"
        )
        info = os.lstat(self.journal_dir)
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or not stat.S_ISDIR(info.st_mode)
        ):
            raise OSError(f"operation journal 目录不安全: {self.journal_dir}")

    @staticmethod
    def _validate_record_file(path: Path) -> None:
        info = os.lstat(path)
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink > 1
        ):
            raise OSError(f"operation journal 记录不安全: {path.name}")

    def path_for(self, operation_id: str) -> Path:
        self._validate_root()
        if not operation_id or any(ch not in "0123456789abcdef-" for ch in operation_id):
            raise ValueError("operation_id 非法")
        return self.journal_dir / f"{operation_id}.json"

    _REQUIRED_RECORD_FIELDS = ("operation_id", "action", "status", "stage")

    @staticmethod
    def _repair_record(path: Path, message: str) -> dict[str, Any]:
        """Stable repair record for unreadable/invalid journal entries."""
        return {
            "operation_id": path.stem,
            "action": "unknown",
            "status": OperationStatus.REPAIR_REQUIRED.value,
            "stage": StorageStage.PREPARING.value,
            "errors": [
                _error(
                    ErrorCode.STORAGE_REPAIR_REQUIRED,
                    message,
                    recoverable=True,
                )
            ],
            "repair_actions": ["repair_operation_journal"],
        }

    @classmethod
    def _validate_payload(
        cls, path: Path, payload: Any
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Return (valid record, None) or (None, reason)."""
        if not isinstance(payload, dict):
            return None, f"operation journal 记录不是 JSON 对象: {path.name}"
        missing = [
            field
            for field in cls._REQUIRED_RECORD_FIELDS
            if not isinstance(payload.get(field), str) or not payload[field]
        ]
        if missing:
            return None, (
                f"operation journal 记录缺少必需字段 "
                f"({', '.join(missing)}): {path.name}"
            )
        operation_id = payload["operation_id"]
        if (
            operation_id != path.stem
            or any(ch not in "0123456789abcdef-" for ch in operation_id)
        ):
            return None, f"operation journal operation_id 与文件名不一致或非法: {path.name}"
        if payload["action"] not in {"archive", "delete", "apply_ai_patch"}:
            return None, f"operation journal action 非法: {path.name}"
        valid_statuses = {status.value for status in OperationStatus} | {"in_progress"}
        if payload["status"] not in valid_statuses:
            return None, f"operation journal status 非法: {path.name}"
        valid_stages = {stage.value for stage in StorageStage}
        if payload["stage"] not in valid_stages:
            return None, f"operation journal stage 非法: {path.name}"
        if not isinstance(payload.get("errors", []), list) or not isinstance(
            payload.get("repair_actions", []), list
        ):
            return None, f"operation journal errors/repair_actions 类型非法: {path.name}"
        if not all(isinstance(item, dict) for item in payload.get("errors", [])):
            return None, f"operation journal errors 元素类型非法: {path.name}"
        if not all(
            isinstance(item, str) and item
            for item in payload.get("repair_actions", [])
        ):
            return None, f"operation journal repair_actions 元素类型非法: {path.name}"

        schema_version = payload.get("journal_schema_version")
        if schema_version is not None and (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version not in {1, 2, 3}
        ):
            return None, f"operation journal schema version 非法: {path.name}"
        for boolean_field in (
            "vector_required",
            "primary_missing",
            "core_committed",
            "sqlite_commit_reconciled",
        ):
            if boolean_field in payload and not isinstance(
                payload[boolean_field], bool
            ):
                return None, (
                    f"operation journal {boolean_field} 类型非法: {path.name}"
                )
        knowledge_id = payload.get("knowledge_id")
        if knowledge_id is not None and not _valid_positive_int(knowledge_id):
            return None, f"operation journal knowledge_id 非法: {path.name}"
        projection_sha256 = payload.get("projection_sha256")
        if projection_sha256 is not None and not _valid_sha256(projection_sha256):
            return None, f"operation journal projection_sha256 非法: {path.name}"
        for digest_field in (
            "primary_sha256",
            "quarantine_sha256",
            "previous_primary_sha256",
        ):
            if digest_field in payload and payload[digest_field] is not None and not _valid_sha256(
                payload[digest_field]
            ):
                return None, f"operation journal {digest_field} 非法: {path.name}"
        for prefix in ("primary", "quarantine", "previous_primary"):
            st_dev = payload.get(f"{prefix}_st_dev")
            st_ino = payload.get(f"{prefix}_st_ino")
            if (st_dev is None) != (st_ino is None):
                return None, f"operation journal {prefix} 身份字段不完整: {path.name}"
            if st_dev is not None and _recorded_file_identity(payload, prefix) is None:
                return None, f"operation journal {prefix} 身份字段非法: {path.name}"

        if schema_version in {2, 3} and payload["status"] == "in_progress":
            checkpoint = payload.get("checkpoint")
            action = payload["action"]
            allowed = {
                "archive": {
                    "journal_created": StorageStage.PREPARING.value,
                    "archive_planned": StorageStage.PREPARING.value,
                    "primary_committed": StorageStage.PRIMARY_COMMITTED.value,
                    "index_committed": StorageStage.INDEX_COMMITTED.value,
                    "archive_compensating": StorageStage.COMPENSATING.value,
                },
                "delete": {
                    "journal_created": StorageStage.PREPARING.value,
                    "delete_planned": StorageStage.DELETE_PLANNED.value,
                    "primary_quarantined": StorageStage.DELETE_QUARANTINED.value,
                },
                "apply_ai_patch": {
                    "journal_created": StorageStage.PREPARING.value,
                    "patch_planned": StorageStage.PREPARING.value,
                    "patch_quarantined": StorageStage.PREPARING.value,
                    "patch_primary_committed": StorageStage.PRIMARY_COMMITTED.value,
                    "patch_index_committed": StorageStage.INDEX_COMMITTED.value,
                    "patch_compensating": StorageStage.COMPENSATING.value,
                },
            }
            if checkpoint not in allowed[action] or allowed[action][checkpoint] != payload["stage"]:
                return None, f"operation journal checkpoint/stage 非法: {path.name}"
            if checkpoint != "journal_created":
                if projection_sha256 is None:
                    return None, f"operation journal 缺少 projection_sha256: {path.name}"
                relative_field = (
                    "planned_relative_file_path"
                    if action == "archive"
                    else "relative_file_path"
                )
                if not isinstance(payload.get(relative_field), str) or not payload[
                    relative_field
                ]:
                    return None, f"operation journal 缺少相对路径: {path.name}"
                if action in {"archive", "delete"} and not isinstance(
                    payload.get("vector_required"), bool
                ):
                    return None, f"operation journal 缺少 vector_required: {path.name}"
            if action == "archive" and checkpoint in {
                "primary_committed",
                "index_committed",
                "archive_compensating",
            }:
                if _recorded_file_identity(payload, "primary") is None:
                    return None, f"operation journal 缺少主文件身份: {path.name}"
                if schema_version == 3 and not _valid_sha256(
                    payload.get("primary_sha256")
                ):
                    return None, f"operation journal 缺少主文件摘要: {path.name}"
            if action == "archive" and checkpoint == "index_committed":
                if not _valid_positive_int(knowledge_id):
                    return None, f"operation journal 缺少 knowledge_id: {path.name}"
            if action == "delete" and checkpoint != "journal_created":
                primary_missing = payload.get("primary_missing")
                if not isinstance(primary_missing, bool):
                    return None, f"operation journal 缺少 primary_missing: {path.name}"
                if not primary_missing and _recorded_file_identity(
                    payload, "primary"
                ) is None:
                    return None, f"operation journal 缺少删除主文件身份: {path.name}"
                if (
                    schema_version == 3
                    and not primary_missing
                    and not _valid_sha256(payload.get("primary_sha256"))
                ):
                    return None, f"operation journal 缺少删除主文件摘要: {path.name}"
            if action == "delete" and checkpoint == "primary_quarantined":
                if not payload.get("primary_missing") and _recorded_file_identity(
                    payload, "quarantine"
                ) is None:
                    return None, f"operation journal 缺少隔离文件身份: {path.name}"
                if (
                    schema_version == 3
                    and not payload.get("primary_missing")
                    and not _valid_sha256(payload.get("quarantine_sha256"))
                ):
                    return None, f"operation journal 缺少隔离文件摘要: {path.name}"
            if action == "apply_ai_patch":
                if (
                    not _valid_positive_int(knowledge_id)
                    or not _valid_sha256(payload.get("previous_revision_sha256"))
                ):
                    return None, f"operation journal patch revision/knowledge 字段非法: {path.name}"
                if checkpoint != "journal_created":
                    if not _valid_sha256(payload.get("resulting_revision_sha256")):
                        return None, f"operation journal patch 缺少 resulting revision: {path.name}"
                    if not isinstance(payload.get("original_path"), str) or not payload[
                        "original_path"
                    ]:
                        return None, f"operation journal patch 缺少原 Markdown 路径: {path.name}"
                    if not isinstance(payload.get("planned_quarantine_path"), str) or not payload[
                        "planned_quarantine_path"
                    ]:
                        return None, f"operation journal patch 缺少隔离计划路径: {path.name}"
                    if _recorded_file_identity(payload, "previous_primary") is None or not _valid_sha256(
                        payload.get("previous_primary_sha256")
                    ):
                        return None, f"operation journal patch 缺少旧主文件身份或摘要: {path.name}"
                if checkpoint == "patch_quarantined":
                    if _recorded_file_identity(payload, "quarantine") is None or not _valid_sha256(
                        payload.get("quarantine_sha256")
                    ):
                        return None, f"operation journal patch 缺少隔离文件身份或摘要: {path.name}"
                if checkpoint in {
                    "patch_primary_committed",
                    "patch_index_committed",
                    "patch_compensating",
                }:
                    for prefix in ("primary", "quarantine"):
                        if _recorded_file_identity(payload, prefix) is None or not _valid_sha256(
                            payload.get(f"{prefix}_sha256")
                        ):
                            return None, f"operation journal patch 缺少 {prefix} 身份或摘要: {path.name}"
        return payload, None

    def _fsync_directory(self) -> None:
        """Make a rename durable on POSIX by fsyncing the parent directory."""
        if os.name != "posix":
            return
        try:
            descriptor = os.open(self.journal_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def write(self, operation_id: str, payload: dict[str, Any]) -> Path:
        target = self.path_for(operation_id)
        document = dict(payload)
        document["operation_id"] = operation_id
        document["updated_at"] = _utc_now()
        valid, reason = self._validate_payload(target, document)
        if valid is None:
            raise ValueError(reason or "operation journal payload 非法")
        encoded = (
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            atomic_publish_file(
                target,
                label="operation journal 记录",
                data=encoded,
            )
        except PKVRuntimeError as exc:
            # Keep the journal writer's public failure surface stable for the
            # coordinator state machine, which compensates durable-side-effect
            # boundaries on OSError.
            raise OSError(f"operation journal 发布失败: {target.name}") from exc
        self._fsync_directory()
        return target

    def read(self, operation_id: str) -> dict[str, Any]:
        """Read one record; non-object or incomplete JSON raises REPAIR_REQUIRED."""
        path = self.path_for(operation_id)
        self._validate_record_file(path)
        with open_user_file_nofollow(
            path,
            "r",
            label="operation journal 记录",
            encoding="utf-8",
        ) as handle:
            payload = json.load(handle)
        valid, reason = self._validate_payload(path, payload)
        if valid is None:
            raise PKVRuntimeError(
                ErrorCode.STORAGE_REPAIR_REQUIRED,
                reason or "operation journal 记录无效",
                recoverable=True,
            )
        return valid

    def records(self) -> Iterable[dict[str, Any]]:
        self._validate_root()
        for path in sorted(self.journal_dir.glob("*.json")):
            try:
                self._validate_record_file(path)
                with open_user_file_nofollow(
                    path,
                    "r",
                    label="operation journal 记录",
                    encoding="utf-8",
                ) as handle:
                    payload = json.load(handle)
            except (OSError, UnicodeError, json.JSONDecodeError, PKVRuntimeError):
                yield self._repair_record(
                    path, f"operation journal 不可读取: {path.name}"
                )
                continue
            valid, reason = self._validate_payload(path, payload)
            if valid is None:
                yield self._repair_record(path, reason or "operation journal 记录无效")
            else:
                yield valid


class StorageCoordinator:
    """Make primary/index outcomes explicit and auxiliary vector failure repairable."""

    def __init__(
        self,
        markdown_store: MarkdownStore,
        sqlite_store: SQLiteStore,
        journal_dir: Path,
    ) -> None:
        self.markdown_store = markdown_store
        self.sqlite_store = sqlite_store
        self.journal = StorageOperationJournal(journal_dir)

    def archive(
        self,
        entry: Entry,
        *,
        operation_id: str | None = None,
        chunks: Optional[list[str]] = None,
        vector_operation: Optional[VectorOperation] = None,
        vector_error: Optional[BaseException] = None,
        vector_required: bool = False,
    ) -> StorageOperationResult:
        operation_id = _operation_id_or_new(operation_id)
        initial = {
            "action": "archive",
            "status": "in_progress",
            "stage": StorageStage.PREPARING.value,
            "journal_schema_version": 3,
            "checkpoint": "journal_created",
            "title": entry.title,
            "errors": [],
            "repair_actions": [],
        }
        try:
            self.journal.write(operation_id, initial)
        except OSError as exc:
            return StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"无法创建存储操作日志: {exc}",
                        recoverable=True,
                    ),
                ),
            )

        # Deterministic no-clobber plan BEFORE any archive file write.  The
        # journal records the exact absolute/relative target so a crashed
        # restart can prove where the primary file should be.
        try:
            plan = self.markdown_store.plan_save(entry)
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"无法规划 Markdown 目标路径: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        try:
            required_chunks = chunks
            if required_chunks is None:
                required_chunks = split_text_into_chunks(entry.content)
            projection_sha256 = entry_projection_sha256(
                entry,
                plan.relative_path,
                required_chunks,
            )
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        f"无法准备 SQLite/chunk 投影: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        planned_payload = {
            **initial,
            "checkpoint": "archive_planned",
            "planned_file_path": str(plan.absolute_path),
            "planned_relative_file_path": plan.relative_path,
            "vector_required": vector_required,
            "projection_sha256": projection_sha256,
        }
        try:
            self.journal.write(operation_id, planned_payload)
        except OSError as exc:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"无法持久化归档计划: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        # A stale or independently-created row must be discovered before the
        # primary file is published.  Later reconciliation never attributes a
        # row by file_path alone; this is only a no-side-effect preflight.
        try:
            existing_row = self.sqlite_store.query_by_file_path(plan.relative_path)
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                relative_file_path=plan.relative_path,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        f"归档前 SQLite 路径查重失败: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)
        if existing_row is not None:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                relative_file_path=plan.relative_path,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        "归档目标 file_path 已存在 SQLite 记录，未写入 Markdown",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        try:
            published_file = self.markdown_store.save_planned_record(plan, entry)
            saved_path = published_file.path
        except FileExistsError:
            # The planned target raced into existence: fail without overwrite
            # and without leaving an orphan temporary file.
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"归档目标并发创建，未覆盖任何文件: {plan.absolute_path}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)
        except PKVRuntimeError as exc:
            if exc.code is ErrorCode.STORAGE_REPAIR_REQUIRED:
                result = StorageOperationResult(
                    operation_id,
                    "archive",
                    OperationStatus.REPAIR_REQUIRED,
                    StorageStage.PRIMARY_COMMITTED,
                    file_path=str(plan.absolute_path),
                    relative_file_path=plan.relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_REPAIR_REQUIRED,
                            f"Markdown 发布结果需要人工核验: {exc}",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=("audit_published_markdown",),
                )
                return self._finish(result)
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"Markdown 主存储失败: {exc}",
                        recoverable=False,
                    ),
                ),
            )
            return self._finish(result)
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"Markdown 主存储失败: {exc}",
                        recoverable=False,
                    ),
                ),
            )
            return self._finish(result)

        relative_path = self.markdown_store.relative_path(saved_path)

        # Preserve the durable plan and vector requirement across checkpoints.
        # Recovery must not report READY when a required vector write has an
        # unknown crash-boundary outcome.
        primary_payload = dict(planned_payload)
        primary_payload.update(
            {
                "checkpoint": "primary_committed",
                "stage": StorageStage.PRIMARY_COMMITTED.value,
                "file_path": str(saved_path),
                "relative_file_path": relative_path,
                "primary_st_dev": published_file.st_dev,
                "primary_st_ino": published_file.st_ino,
                "primary_sha256": published_file.sha256,
            }
        )
        try:
            self.journal.write(operation_id, primary_payload)
        except OSError as journal_error:
            try:
                removed = self.markdown_store.gateway.delete_if_identity(
                    saved_path,
                    expected_identity=published_file.identity,
                    expected_sha256=published_file.sha256,
                )
                if not removed:
                    raise FileNotFoundError(saved_path)
            except Exception as compensation_error:
                result = StorageOperationResult(
                    operation_id,
                    "archive",
                    OperationStatus.REPAIR_REQUIRED,
                    StorageStage.COMPENSATING,
                    file_path=str(saved_path),
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_REPAIR_REQUIRED,
                            f"主存储已写入但日志更新失败: {journal_error}",
                            recoverable=True,
                        ),
                        _error(
                            ErrorCode.STORAGE_COMPENSATION_FAILED,
                            f"Markdown 补偿删除失败: {compensation_error}",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=("remove_or_reindex_orphan_markdown",),
                )
            else:
                result = StorageOperationResult(
                    operation_id,
                    "archive",
                    OperationStatus.REJECTED,
                    StorageStage.COMPENSATING,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_PRIMARY_FAILED,
                            f"operation journal 更新失败，主存储已补偿: {journal_error}",
                            recoverable=True,
                        ),
                    ),
                )
            return self._finish(result)

        sqlite_commit_reconciled = False
        try:
            knowledge_id = self.sqlite_store.insert_entry_with_chunks(
                entry,
                relative_path,
                required_chunks,
                operation_id=operation_id,
                projection_sha256=projection_sha256,
            )
        except Exception as index_error:
            probe = _probe_archive_commit(
                self.sqlite_store,
                operation_id=operation_id,
                relative_file_path=relative_path,
                projection_sha256=projection_sha256,
            )
            if probe.state == "committed" and probe.knowledge_id is not None:
                # The SQLite transaction committed and its operation-bound proof
                # matches the exact row/chunk projection.  Never compensate the
                # already-committed Markdown in this state.
                knowledge_id = probe.knowledge_id
                sqlite_commit_reconciled = True
            elif probe.state == "ambiguous":
                result = StorageOperationResult(
                    operation_id,
                    "archive",
                    OperationStatus.REPAIR_REQUIRED,
                    StorageStage.PRIMARY_COMMITTED,
                    file_path=str(saved_path),
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_INDEX_FAILED,
                            f"SQLite/FTS/chunk 写入返回异常: {index_error}",
                            recoverable=True,
                        ),
                        _error(
                            ErrorCode.STORAGE_REPAIR_REQUIRED,
                            f"SQLite 提交状态无法安全归因: {probe.reason}",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=(
                        "audit_sqlite_commit_state",
                        "audit_entry_consistency",
                    ),
                )
                return self._finish(result)
            else:
                # The operation proof is absent and no row occupies this exact
                # path.  The SQLite transaction did not commit; compensating the
                # operation-bound Markdown identity is safe.
                try:
                    self.journal.write(
                        operation_id,
                        {
                            **primary_payload,
                            "checkpoint": "archive_compensating",
                            "stage": StorageStage.COMPENSATING.value,
                            "errors": [
                                _error(
                                    ErrorCode.STORAGE_INDEX_FAILED,
                                    f"SQLite/FTS/chunk 写入失败: {index_error}",
                                    recoverable=True,
                                )
                            ],
                        },
                    )
                except OSError:
                    pass
                try:
                    removed = self.markdown_store.gateway.delete_if_identity(
                        saved_path,
                        expected_identity=published_file.identity,
                        expected_sha256=published_file.sha256,
                    )
                    if not removed:
                        raise FileNotFoundError(saved_path)
                except Exception as compensation_error:
                    result = StorageOperationResult(
                        operation_id,
                        "archive",
                        OperationStatus.REPAIR_REQUIRED,
                        StorageStage.COMPENSATING,
                        file_path=str(saved_path),
                        relative_file_path=relative_path,
                        errors=(
                            _error(
                                ErrorCode.STORAGE_INDEX_FAILED,
                                f"SQLite/FTS/chunk 写入失败: {index_error}",
                                recoverable=True,
                            ),
                            _error(
                                ErrorCode.STORAGE_COMPENSATION_FAILED,
                                f"Markdown 补偿删除失败: {compensation_error}",
                                recoverable=True,
                            ),
                        ),
                        repair_actions=("remove_or_reindex_orphan_markdown",),
                    )
                else:
                    result = StorageOperationResult(
                        operation_id,
                        "archive",
                        OperationStatus.REJECTED,
                        StorageStage.COMPENSATING,
                        errors=(
                            _error(
                                ErrorCode.STORAGE_INDEX_FAILED,
                                f"SQLite/FTS/chunk 写入失败，Markdown 已补偿: {index_error}",
                                recoverable=True,
                            ),
                        ),
                    )
                return self._finish(result)

        indexed_payload = {
            **primary_payload,
            "checkpoint": "index_committed",
            "stage": StorageStage.INDEX_COMMITTED.value,
            "knowledge_id": knowledge_id,
            "sqlite_commit_reconciled": sqlite_commit_reconciled,
        }
        try:
            self.journal.write(operation_id, indexed_payload)
        except OSError as exc:
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.REPAIR_REQUIRED,
                StorageStage.INDEX_COMMITTED,
                knowledge_id=knowledge_id,
                file_path=str(saved_path),
                relative_file_path=relative_path,
                core_committed=True,
                errors=(
                    _error(
                        ErrorCode.STORAGE_REPAIR_REQUIRED,
                        f"核心存储已提交但操作日志更新失败: {exc}",
                        recoverable=True,
                    ),
                ),
                repair_actions=("audit_entry_consistency", "rebuild_vectors_for_entry"),
            )
            return self._finish(result)

        if vector_error is not None or (vector_required and vector_operation is None):
            message = (
                f"向量准备失败: {vector_error}"
                if vector_error is not None
                else "向量操作缺失"
            )
            result = StorageOperationResult(
                operation_id,
                "archive",
                OperationStatus.DEGRADED,
                StorageStage.INDEX_COMMITTED,
                knowledge_id=knowledge_id,
                file_path=str(saved_path),
                relative_file_path=relative_path,
                core_committed=True,
                errors=(
                    _error(
                        ErrorCode.STORAGE_VECTOR_FAILED,
                        message,
                        recoverable=True,
                    ),
                ),
                repair_actions=("rebuild_vectors_for_entry",),
            )
            return self._finish(result)

        if vector_operation is not None:
            try:
                vector_operation(knowledge_id)
            except Exception as exc:
                result = StorageOperationResult(
                    operation_id,
                    "archive",
                    OperationStatus.DEGRADED,
                    StorageStage.INDEX_COMMITTED,
                    knowledge_id=knowledge_id,
                    file_path=str(saved_path),
                    relative_file_path=relative_path,
                    core_committed=True,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_VECTOR_FAILED,
                            f"向量辅助索引失败: {exc}",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=("rebuild_vectors_for_entry",),
                )
                return self._finish(result)

        result = StorageOperationResult(
            operation_id,
            "archive",
            OperationStatus.READY,
            StorageStage.COMPLETED,
            knowledge_id=knowledge_id,
            file_path=str(saved_path),
            relative_file_path=relative_path,
            core_committed=True,
        )
        return self._finish(result)

    def delete(
        self,
        knowledge_id: int,
        *,
        operation_id: str | None = None,
        vector_operation: Optional[VectorOperation] = None,
    ) -> StorageOperationResult:
        operation_id = _operation_id_or_new(operation_id)
        if not _valid_positive_int(knowledge_id):
            return StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        "knowledge_id 必须为正整数",
                        recoverable=False,
                    ),
                ),
            )
        initial = {
            "action": "delete",
            "status": "in_progress",
            "stage": StorageStage.PREPARING.value,
            "journal_schema_version": 3,
            "checkpoint": "journal_created",
            "knowledge_id": knowledge_id,
            "errors": [],
            "repair_actions": [],
        }
        try:
            self.journal.write(operation_id, initial)
        except OSError as exc:
            return StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"无法创建删除操作日志: {exc}",
                        recoverable=True,
                    ),
                ),
            )

        try:
            row = self.sqlite_store.query_by_id(knowledge_id)
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        f"删除前读取 SQLite 记录失败: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)
        if row is None:
            result = StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        "数据库记录不存在",
                        recoverable=False,
                    ),
                ),
            )
            return self._finish(result)

        try:
            source_chunks = self.sqlite_store.get_chunks_by_knowledge_id(knowledge_id)
            projection_sha256 = row_projection_sha256(row, source_chunks)
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                errors=(
                    _error(
                        ErrorCode.STORAGE_INDEX_FAILED,
                        f"删除前读取 SQLite/chunk 投影失败: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        relative_path = str(row.get("file_path") or "")
        quarantine: Optional[QuarantinedVaultFile] = None
        primary_missing = False
        original_path: Optional[Path] = None
        original_identity: tuple[int, int] | None = None
        original_sha256: str | None = None
        try:
            original_path = self.markdown_store.gateway.resolve(
                relative_path, must_exist=True, require_file=True
            )
            (
                original_identity,
                original_sha256,
            ) = self.markdown_store.gateway.file_fingerprint(original_path)
        except FileNotFoundError:
            primary_missing = True
        except Exception as exc:
            result = StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                relative_file_path=relative_path,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"Markdown 路径拒绝: {exc}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        # Deterministic quarantine target derived from the operation id,
        # journaled BEFORE any quarantine move.
        planned_quarantine_path: Optional[Path] = None
        if not primary_missing:
            try:
                planned_quarantine_path = self.markdown_store.plan_quarantine(
                    relative_path, operation_id
                )
            except Exception as exc:
                result = StorageOperationResult(
                    operation_id,
                    "delete",
                    OperationStatus.REJECTED,
                    StorageStage.PREPARING,
                    knowledge_id=knowledge_id,
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_PRIMARY_FAILED,
                            f"无法规划隔离路径: {exc}",
                            recoverable=True,
                        ),
                    ),
                )
                return self._finish(result)

        planned_payload = {
            **initial,
            "checkpoint": "delete_planned",
            "stage": StorageStage.DELETE_PLANNED.value,
            "relative_file_path": relative_path,
            "original_path": str(original_path) if original_path is not None else None,
            "planned_quarantine_path": (
                str(planned_quarantine_path)
                if planned_quarantine_path is not None
                else None
            ),
            "primary_missing": primary_missing,
            "primary_st_dev": original_identity[0] if original_identity else None,
            "primary_st_ino": original_identity[1] if original_identity else None,
            "primary_sha256": original_sha256,
            "projection_sha256": projection_sha256,
            # A supplied callback means auxiliary vector cleanup is part of the
            # requested delete.  Preserve that fact before any move so restart
            # recovery cannot silently report DELETED while vector state is
            # unknown at the crash boundary.
            "vector_required": vector_operation is not None,
        }
        try:
            self.journal.write(operation_id, planned_payload)
        except OSError as journal_error:
            # Nothing was moved yet, so no compensation is required.
            result = StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=knowledge_id,
                relative_file_path=relative_path,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"删除计划日志更新失败，未产生任何副作用: {journal_error}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        if not primary_missing:
            try:
                quarantine = self.markdown_store.quarantine(
                    relative_path,
                    operation_id=operation_id,
                    expected_identity=original_identity,
                    expected_sha256=original_sha256,
                )
            except PKVRuntimeError as exc:
                if exc.code is ErrorCode.STORAGE_COMPENSATION_FAILED:
                    # Post-move validation failed AND restore failed: the
                    # primary file sits at the recorded quarantine path.
                    result = StorageOperationResult(
                        operation_id,
                        "delete",
                        OperationStatus.REPAIR_REQUIRED,
                        StorageStage.COMPENSATING,
                        knowledge_id=knowledge_id,
                        relative_file_path=relative_path,
                        original_path=(
                            str(original_path) if original_path is not None else None
                        ),
                        quarantine_path=(
                            str(planned_quarantine_path)
                            if planned_quarantine_path is not None
                            else None
                        ),
                        errors=(
                            _error(
                                ErrorCode.STORAGE_REPAIR_REQUIRED,
                                f"隔离移动后校验失败且恢复失败: {exc}",
                                recoverable=True,
                            ),
                        ),
                        repair_actions=("restore_quarantined_markdown",),
                    )
                    return self._finish(result)
                # Nothing moved, or the move was rolled back successfully.
                result = StorageOperationResult(
                    operation_id,
                    "delete",
                    OperationStatus.REJECTED,
                    StorageStage.PREPARING,
                    knowledge_id=knowledge_id,
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_PRIMARY_FAILED,
                            f"Markdown 隔离失败（无副作用或已恢复）: {exc}",
                            recoverable=True,
                        ),
                    ),
                )
                return self._finish(result)
            except Exception as exc:
                result = StorageOperationResult(
                    operation_id,
                    "delete",
                    OperationStatus.REJECTED,
                    StorageStage.PREPARING,
                    knowledge_id=knowledge_id,
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_PRIMARY_FAILED,
                            f"Markdown 隔离失败: {exc}",
                            recoverable=True,
                        ),
                    ),
                )
                return self._finish(result)

        quarantined_payload = {
            **planned_payload,
            "checkpoint": "primary_quarantined",
            "stage": StorageStage.DELETE_QUARANTINED.value,
            "relative_file_path": relative_path,
            "primary_missing": primary_missing,
            "quarantine_path": str(quarantine.quarantine_path) if quarantine else None,
            "original_path": (
                str(quarantine.original_path)
                if quarantine
                else (str(original_path) if original_path is not None else None)
            ),
            "quarantine_st_dev": quarantine.st_dev if quarantine else None,
            "quarantine_st_ino": quarantine.st_ino if quarantine else None,
            "quarantine_sha256": quarantine.sha256 if quarantine else None,
        }
        try:
            self.journal.write(operation_id, quarantined_payload)
        except OSError as journal_error:
            if quarantine is not None:
                try:
                    self.markdown_store.restore(quarantine)
                except Exception as compensation_error:
                    result = StorageOperationResult(
                        operation_id,
                        "delete",
                        OperationStatus.REPAIR_REQUIRED,
                        StorageStage.COMPENSATING,
                        knowledge_id=knowledge_id,
                        relative_file_path=relative_path,
                        original_path=str(quarantine.original_path),
                        quarantine_path=str(quarantine.quarantine_path),
                        errors=(
                            _error(
                                ErrorCode.STORAGE_REPAIR_REQUIRED,
                                f"删除日志更新失败: {journal_error}",
                                recoverable=True,
                            ),
                            _error(
                                ErrorCode.STORAGE_COMPENSATION_FAILED,
                                f"隔离文件恢复失败: {compensation_error}",
                                recoverable=True,
                            ),
                        ),
                        repair_actions=("restore_quarantined_markdown",),
                    )
                    return self._finish(result)
            result = StorageOperationResult(
                operation_id,
                "delete",
                OperationStatus.REJECTED,
                StorageStage.COMPENSATING,
                knowledge_id=knowledge_id,
                relative_file_path=relative_path,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"删除日志更新失败，隔离已回滚: {journal_error}",
                        recoverable=True,
                    ),
                ),
            )
            return self._finish(result)

        try:
            deleted = self.sqlite_store.delete_entry(
                knowledge_id,
                operation_id=operation_id,
                projection_sha256=projection_sha256,
                relative_file_path=relative_path,
            )
            if not deleted:
                raise RuntimeError("数据库记录在删除事务前消失")
        except Exception as index_error:
            probe = _probe_delete_commit(
                self.sqlite_store,
                operation_id=operation_id,
                knowledge_id=knowledge_id,
                relative_file_path=relative_path,
                projection_sha256=projection_sha256,
            )
            if probe.state == "committed":
                # Commit proof and row absence agree: the delete transaction
                # completed even though the call returned an exception.
                pass
            elif probe.state == "ambiguous":
                result = StorageOperationResult(
                    operation_id,
                    "delete",
                    OperationStatus.REPAIR_REQUIRED,
                    StorageStage.DELETE_QUARANTINED,
                    knowledge_id=knowledge_id,
                    relative_file_path=relative_path,
                    original_path=(
                        str(quarantine.original_path)
                        if quarantine is not None
                        else (str(original_path) if original_path is not None else None)
                    ),
                    quarantine_path=(
                        str(quarantine.quarantine_path)
                        if quarantine is not None
                        else None
                    ),
                    errors=(
                        _error(
                            ErrorCode.STORAGE_INDEX_FAILED,
                            f"SQLite 删除返回异常: {index_error}",
                            recoverable=True,
                        ),
                        _error(
                            ErrorCode.STORAGE_REPAIR_REQUIRED,
                            f"SQLite 删除提交状态无法安全归因: {probe.reason}",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=("audit_delete_commit_state",),
                )
                return self._finish(result)
            elif primary_missing:
                # The primary file was already missing before this delete;
                # there is nothing to restore and we must not claim a restore
                # happened.  This is a repair condition requiring an audit.
                result = StorageOperationResult(
                    operation_id,
                    "delete",
                    OperationStatus.REPAIR_REQUIRED,
                    StorageStage.COMPENSATING,
                    knowledge_id=knowledge_id,
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_INDEX_FAILED,
                            f"SQLite 删除失败: {index_error}",
                            recoverable=True,
                        ),
                        _error(
                            ErrorCode.STORAGE_REPAIR_REQUIRED,
                            "删除前 Markdown 主文件已缺失；主文件未被恢复（没有可恢复的文件）",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=("audit_missing_primary_file",),
                )
                return self._finish(result)
            else:
                try:
                    if quarantine is not None:
                        self.markdown_store.restore(quarantine)
                except Exception as compensation_error:
                    result = StorageOperationResult(
                        operation_id,
                        "delete",
                        OperationStatus.REPAIR_REQUIRED,
                        StorageStage.COMPENSATING,
                        knowledge_id=knowledge_id,
                        relative_file_path=relative_path,
                        original_path=(
                            str(quarantine.original_path) if quarantine else None
                        ),
                        quarantine_path=(
                            str(quarantine.quarantine_path) if quarantine else None
                        ),
                        errors=(
                            _error(
                                ErrorCode.STORAGE_INDEX_FAILED,
                                f"SQLite 删除失败: {index_error}",
                                recoverable=True,
                            ),
                            _error(
                                ErrorCode.STORAGE_COMPENSATION_FAILED,
                                f"Markdown 恢复失败: {compensation_error}",
                                recoverable=True,
                            ),
                        ),
                        repair_actions=("restore_quarantined_markdown",),
                    )
                else:
                    result = StorageOperationResult(
                        operation_id,
                        "delete",
                        OperationStatus.REJECTED,
                        StorageStage.COMPENSATING,
                        knowledge_id=knowledge_id,
                        relative_file_path=relative_path,
                        errors=(
                            _error(
                                ErrorCode.STORAGE_INDEX_FAILED,
                                f"SQLite 删除失败，Markdown 已恢复: {index_error}",
                                recoverable=True,
                            ),
                        ),
                    )
                return self._finish(result)

        errors: list[dict[str, Any]] = []
        repairs: list[str] = []
        if primary_missing:
            errors.append(
                _error(
                    ErrorCode.STORAGE_REPAIR_REQUIRED,
                    "删除前 Markdown 主文件已缺失",
                    recoverable=True,
                )
            )
            repairs.append("audit_missing_primary_file")
        if vector_operation is not None:
            try:
                vector_operation(knowledge_id)
            except Exception as exc:
                errors.append(
                    _error(
                        ErrorCode.STORAGE_VECTOR_FAILED,
                        f"向量辅助索引删除失败: {exc}",
                        recoverable=True,
                    )
                )
                repairs.append("remove_stale_vectors_for_entry")

        if quarantine is not None:
            try:
                self.markdown_store.finalize_quarantine(quarantine)
            except Exception as exc:
                errors.append(
                    _error(
                        ErrorCode.STORAGE_REPAIR_REQUIRED,
                        f"隔离文件清理失败: {exc}",
                        recoverable=True,
                    )
                )
                repairs.append("purge_committed_quarantine")

        status = OperationStatus.DEGRADED if errors else OperationStatus.DELETED
        result = StorageOperationResult(
            operation_id,
            "delete",
            status,
            StorageStage.COMPLETED,
            knowledge_id=knowledge_id,
            relative_file_path=relative_path,
            core_committed=True,
            errors=tuple(errors),
            repair_actions=tuple(dict.fromkeys(repairs)),
        )
        return self._finish(result)

    def apply_ai_patch(
        self,
        patch: DerivationPatch,
        *,
        operation_id: str | None = None,
    ) -> StorageOperationResult:
        """Commit one revision-bound DerivationPatch through the Q1′ writer.

        The old Markdown primary is quarantined before the patched replacement is
        published.  SQLite then verifies the exact prior projection and records
        an operation proof in the R4 table.  A failed SQLite half restores the
        quarantined primary whenever its identities remain provable.
        """

        if not isinstance(patch, DerivationPatch):
            raise TypeError("patch 必须是 DerivationPatch")
        operation_id = _operation_id_or_new(operation_id)
        initial = {
            "action": "apply_ai_patch",
            "status": "in_progress",
            "stage": StorageStage.PREPARING.value,
            "journal_schema_version": 3,
            "checkpoint": "journal_created",
            "knowledge_id": patch.target_knowledge_id,
            "previous_revision_sha256": patch.expected_revision_sha256,
            "errors": [],
            "repair_actions": [],
        }
        try:
            self.journal.write(operation_id, initial)
        except OSError as exc:
            return StorageOperationResult(
                operation_id,
                "apply_ai_patch",
                OperationStatus.REJECTED,
                StorageStage.PREPARING,
                knowledge_id=patch.target_knowledge_id,
                errors=(
                    _error(
                        ErrorCode.STORAGE_PRIMARY_FAILED,
                        f"无法创建 patch 操作日志: {exc}",
                        recoverable=True,
                    ),
                ),
            )

        existing_proof = self.sqlite_store.query_r4_content_operation(operation_id)
        if existing_proof is not None:
            if (
                existing_proof.get("action") != "apply_ai_patch"
                or existing_proof.get("knowledge_id") != patch.target_knowledge_id
                or existing_proof.get("previous_revision_sha256")
                != patch.expected_revision_sha256
            ):
                return self._finish(
                    StorageOperationResult(
                        operation_id,
                        "apply_ai_patch",
                        OperationStatus.REPAIR_REQUIRED,
                        StorageStage.INDEX_COMMITTED,
                        knowledge_id=patch.target_knowledge_id,
                        errors=(
                            _error(
                                ErrorCode.STORAGE_REPAIR_REQUIRED,
                                "已有 DerivationPatch 提交凭据与当前 patch 不一致。",
                                recoverable=True,
                            ),
                        ),
                        repair_actions=("audit_patch_consistency",),
                    )
                )
            try:
                self.journal.write(
                    operation_id,
                    {
                        **initial,
                        "relative_file_path": existing_proof.get("relative_file_path"),
                        "projection_sha256": existing_proof.get(
                            "resulting_revision_sha256"
                        ),
                        "core_committed": True,
                    },
                )
            except OSError:
                pass
            return self._finish(
                StorageOperationResult(
                    operation_id,
                    "apply_ai_patch",
                    OperationStatus.READY,
                    StorageStage.COMPLETED,
                    knowledge_id=patch.target_knowledge_id,
                    relative_file_path=existing_proof.get("relative_file_path"),
                    core_committed=True,
                )
            )

        try:
            row = self.sqlite_store.query_by_id(patch.target_knowledge_id)
            if row is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "DerivationPatch 的目标条目已不存在。",
                    stage="r4_patch_target",
                    recoverable=True,
                )
            relative_path = row.get("file_path")
            if not isinstance(relative_path, str) or not relative_path:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "DerivationPatch 目标缺少 Vault 相对路径。",
                    stage="r4_patch_target",
                    recoverable=True,
                )
            chunks = self.sqlite_store.get_chunks_by_knowledge_id(patch.target_knowledge_id)
            if row_projection_sha256(row, chunks) != patch.expected_revision_sha256:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "DerivationPatch 目标 revision 已变化。",
                    stage="r4_patch_target",
                    recoverable=True,
                )
            original_path = self.markdown_store.gateway.resolve(
                relative_path,
                must_exist=True,
                require_file=True,
            )
            original_identity, original_sha256 = (
                self.markdown_store.gateway.file_fingerprint(original_path)
            )
            planned_quarantine_path = self.markdown_store.gateway.plan_quarantine_path(
                original_path,
                operation_id=operation_id,
            )
            original = self.markdown_store.load(original_path)
            chunk_texts = [str(item["chunk_text"]) for item in chunks]
            if entry_projection_sha256(original, relative_path, chunk_texts) != patch.expected_revision_sha256:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "DerivationPatch 的 Markdown 与 SQLite 投影不一致。",
                    stage="r4_patch_target",
                    recoverable=True,
                )
            patched = replace(
                original,
                abstract=patch.summary,
                summary_one_sentence=self._first_sentence(patch.summary),
                summary_100_words=patch.summary,
                tags=list(patch.tags),
            )
            resulting_revision = entry_projection_sha256(
                patched,
                relative_path,
                chunk_texts,
            )
        except PKVRuntimeError as error:
            return self._finish(
                StorageOperationResult(
                    operation_id,
                    "apply_ai_patch",
                    OperationStatus.REJECTED,
                    StorageStage.PREPARING,
                    knowledge_id=patch.target_knowledge_id,
                    errors=(
                        _error(error.code, str(error), recoverable=error.recoverable),
                    ),
                )
            )
        except Exception as exc:
            return self._finish(
                StorageOperationResult(
                    operation_id,
                    "apply_ai_patch",
                    OperationStatus.REJECTED,
                    StorageStage.PREPARING,
                    knowledge_id=patch.target_knowledge_id,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_PRIMARY_FAILED,
                            f"无法准备 DerivationPatch: {exc}",
                            recoverable=True,
                        ),
                    ),
                )
            )

        planned_payload = {
            **initial,
            "checkpoint": "patch_planned",
            "relative_file_path": relative_path,
            "original_path": str(original_path),
            "planned_quarantine_path": str(planned_quarantine_path),
            "previous_primary_st_dev": original_identity[0],
            "previous_primary_st_ino": original_identity[1],
            "previous_primary_sha256": original_sha256,
            "resulting_revision_sha256": resulting_revision,
            "projection_sha256": resulting_revision,
        }
        try:
            self.journal.write(operation_id, planned_payload)
        except OSError as exc:
            return self._finish(
                StorageOperationResult(
                    operation_id,
                    "apply_ai_patch",
                    OperationStatus.REJECTED,
                    StorageStage.PREPARING,
                    knowledge_id=patch.target_knowledge_id,
                    relative_file_path=relative_path,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_PRIMARY_FAILED,
                            f"无法持久化 DerivationPatch 计划: {exc}",
                            recoverable=True,
                        ),
                    ),
                )
            )

        quarantined = None
        published_file = None
        try:
            quarantined = self.markdown_store.gateway.quarantine(
                original_path,
                operation_id=operation_id,
                expected_identity=original_identity,
                expected_sha256=original_sha256,
            )
            quarantined_payload = {
                **planned_payload,
                "checkpoint": "patch_quarantined",
                "quarantine_path": str(quarantined.quarantine_path),
                "quarantine_st_dev": quarantined.st_dev,
                "quarantine_st_ino": quarantined.st_ino,
                "quarantine_sha256": quarantined.sha256,
            }
            self.journal.write(operation_id, quarantined_payload)
            published_file = self.markdown_store.save_planned_record(
                PlannedVaultWrite(original_path, relative_path),
                patched,
            )
            primary_payload = {
                **quarantined_payload,
                "stage": StorageStage.PRIMARY_COMMITTED.value,
                "checkpoint": "patch_primary_committed",
                "file_path": str(original_path),
                "primary_st_dev": published_file.st_dev,
                "primary_st_ino": published_file.st_ino,
                "primary_sha256": published_file.sha256,
                "quarantine_path": str(quarantined.quarantine_path),
                "quarantine_st_dev": quarantined.st_dev,
                "quarantine_st_ino": quarantined.st_ino,
                "quarantine_sha256": quarantined.sha256,
            }
            self.journal.write(operation_id, primary_payload)
        except Exception as exc:
            errors = [
                _error(
                    ErrorCode.STORAGE_PRIMARY_FAILED,
                    f"DerivationPatch Markdown 提交失败: {exc}",
                    recoverable=True,
                )
            ]
            repairs: tuple[str, ...] = ()
            if quarantined is not None and published_file is None:
                try:
                    self.markdown_store.gateway.restore(quarantined)
                except Exception as restore_error:
                    errors.append(
                        _error(
                            ErrorCode.STORAGE_COMPENSATION_FAILED,
                            f"DerivationPatch 原 Markdown 恢复失败: {restore_error}",
                            recoverable=True,
                        )
                    )
                    repairs = ("restore_patch_quarantine",)
            elif published_file is not None and quarantined is not None:
                try:
                    self.markdown_store.gateway.delete_if_identity(
                        original_path,
                        expected_identity=published_file.identity,
                        expected_sha256=published_file.sha256,
                    )
                    self.markdown_store.gateway.restore(quarantined)
                except Exception as restore_error:
                    errors.append(
                        _error(
                            ErrorCode.STORAGE_COMPENSATION_FAILED,
                            f"DerivationPatch 补偿恢复失败: {restore_error}",
                            recoverable=True,
                        )
                    )
                    repairs = ("restore_patch_quarantine",)
            return self._finish(
                StorageOperationResult(
                    operation_id,
                    "apply_ai_patch",
                    OperationStatus.REPAIR_REQUIRED if repairs else OperationStatus.REJECTED,
                    StorageStage.COMPENSATING if repairs else StorageStage.PREPARING,
                    knowledge_id=patch.target_knowledge_id,
                    relative_file_path=relative_path,
                    errors=tuple(errors),
                    repair_actions=repairs,
                )
            )

        sqlite_commit_reconciled = False
        try:
            self.sqlite_store.apply_ai_patch(
                operation_id=operation_id,
                knowledge_id=patch.target_knowledge_id,
                relative_file_path=relative_path,
                previous_revision_sha256=patch.expected_revision_sha256,
                resulting_revision_sha256=resulting_revision,
                entry=patched,
            )
        except Exception as exc:
            probe = _probe_patch_commit(
                self.sqlite_store,
                operation_id=operation_id,
                knowledge_id=patch.target_knowledge_id,
                relative_file_path=relative_path,
                previous_revision_sha256=patch.expected_revision_sha256,
                resulting_revision_sha256=resulting_revision,
            )
            if probe.state == "committed":
                # A transaction-bound proof shows that the SQLite half made it
                # through despite the caller seeing an exception.  The patched
                # Markdown must remain paired with that committed row.
                sqlite_commit_reconciled = True
            elif probe.state == "ambiguous":
                return self._finish(
                    StorageOperationResult(
                        operation_id,
                        "apply_ai_patch",
                        OperationStatus.REPAIR_REQUIRED,
                        StorageStage.PRIMARY_COMMITTED,
                        knowledge_id=patch.target_knowledge_id,
                        relative_file_path=relative_path,
                        errors=(
                            _error(
                                ErrorCode.STORAGE_INDEX_FAILED,
                                f"DerivationPatch SQLite 提交返回异常: {exc}",
                                recoverable=True,
                            ),
                            _error(
                                ErrorCode.STORAGE_REPAIR_REQUIRED,
                                f"DerivationPatch SQLite 提交状态无法安全归因: {probe.reason}",
                                recoverable=True,
                            ),
                        ),
                        repair_actions=("audit_patch_consistency",),
                    )
                )
            else:
                try:
                    self.journal.write(
                        operation_id,
                        {
                            **primary_payload,
                            "stage": StorageStage.COMPENSATING.value,
                            "checkpoint": "patch_compensating",
                            "errors": [
                                _error(
                                    ErrorCode.STORAGE_INDEX_FAILED,
                                    f"DerivationPatch SQLite 提交失败: {exc}",
                                    recoverable=True,
                                )
                            ],
                        },
                    )
                except OSError:
                    pass
                try:
                    assert published_file is not None and quarantined is not None
                    removed = self.markdown_store.gateway.delete_if_identity(
                        original_path,
                        expected_identity=published_file.identity,
                        expected_sha256=published_file.sha256,
                    )
                    if not removed:
                        raise FileNotFoundError(original_path)
                    self.markdown_store.gateway.restore(quarantined)
                except Exception as compensation_error:
                    return self._finish(
                        StorageOperationResult(
                            operation_id,
                            "apply_ai_patch",
                            OperationStatus.REPAIR_REQUIRED,
                            StorageStage.COMPENSATING,
                            knowledge_id=patch.target_knowledge_id,
                            relative_file_path=relative_path,
                            errors=(
                                _error(
                                    ErrorCode.STORAGE_INDEX_FAILED,
                                    f"DerivationPatch SQLite 提交失败: {exc}",
                                    recoverable=True,
                                ),
                                _error(
                                    ErrorCode.STORAGE_COMPENSATION_FAILED,
                                    f"DerivationPatch Markdown 恢复失败: {compensation_error}",
                                    recoverable=True,
                                ),
                            ),
                            repair_actions=("restore_patch_quarantine",),
                        )
                    )
                return self._finish(
                    StorageOperationResult(
                        operation_id,
                        "apply_ai_patch",
                        OperationStatus.REJECTED,
                        StorageStage.COMPENSATING,
                        knowledge_id=patch.target_knowledge_id,
                        relative_file_path=relative_path,
                        errors=(
                            _error(
                                ErrorCode.STORAGE_INDEX_FAILED,
                                f"DerivationPatch SQLite 提交失败，Markdown 已恢复: {exc}",
                                recoverable=True,
                            ),
                        ),
                    )
                )

        indexed_payload = {
            **primary_payload,
            "stage": StorageStage.INDEX_COMMITTED.value,
            "checkpoint": "patch_index_committed",
            "projection_sha256": resulting_revision,
            "core_committed": True,
            "sqlite_commit_reconciled": sqlite_commit_reconciled,
        }
        try:
            self.journal.write(operation_id, indexed_payload)
        except OSError as exc:
            return self._finish(
                StorageOperationResult(
                    operation_id,
                    "apply_ai_patch",
                    OperationStatus.REPAIR_REQUIRED,
                    StorageStage.INDEX_COMMITTED,
                    knowledge_id=patch.target_knowledge_id,
                    relative_file_path=relative_path,
                    core_committed=True,
                    errors=(
                        _error(
                            ErrorCode.STORAGE_REPAIR_REQUIRED,
                            f"DerivationPatch 核心提交后日志更新失败: {exc}",
                            recoverable=True,
                        ),
                    ),
                    repair_actions=("audit_patch_consistency",),
                )
            )

        errors: tuple[dict[str, Any], ...] = ()
        repairs: tuple[str, ...] = ()
        try:
            assert quarantined is not None
            self.markdown_store.gateway.finalize_quarantine(quarantined)
        except Exception as exc:
            errors = (
                _error(
                    ErrorCode.STORAGE_REPAIR_REQUIRED,
                    f"DerivationPatch 已提交但旧 Markdown 隔离副本待清理: {exc}",
                    recoverable=True,
                ),
            )
            repairs = ("purge_patch_quarantine",)
        return self._finish(
            StorageOperationResult(
                operation_id,
                "apply_ai_patch",
                OperationStatus.DEGRADED if errors else OperationStatus.READY,
                StorageStage.COMPLETED,
                knowledge_id=patch.target_knowledge_id,
                relative_file_path=relative_path,
                core_committed=True,
                errors=errors,
                repair_actions=repairs,
            )
        )

    @staticmethod
    def _first_sentence(summary: str) -> str:
        for delimiter in ("。", ".", "！", "!", "？", "?"):
            if delimiter in summary:
                return summary.split(delimiter, 1)[0].strip()
        return summary.strip()

    def pending_repairs(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.journal.records()
            if record.get("status")
            in {
                OperationStatus.DEGRADED.value,
                OperationStatus.REPAIR_REQUIRED.value,
                "in_progress",
            }
        ]

    def _finish(self, result: StorageOperationResult) -> StorageOperationResult:
        payload = result.to_dict()
        try:
            previous = self.journal.read(result.operation_id)
        except (OSError, ValueError, json.JSONDecodeError, PKVRuntimeError):
            # The terminal result remains sufficient to replace an unreadable
            # record for this operation.  If the write also fails, the caller
            # receives REPAIR_REQUIRED below.
            pass
        else:
            # Preserve crash-recovery facts that are not part of the public
            # result envelope (exact plan/quarantine paths, checkpoint and
            # vector_required).  Replacing them with result.to_dict() alone can
            # make a later restart falsely promote an unknown vector outcome to
            # READY.
            for key, value in payload.items():
                # ``StorageOperationResult`` intentionally has a compact public
                # envelope.  Its optional fields default to ``None`` (and
                # core_committed to False), so blindly updating would erase the
                # operation-bound paths/identities retained by an in-progress
                # journal exactly when restart recovery needs them.
                if value is None and key in previous:
                    continue
                if key == "core_committed" and previous.get(key) is True and value is False:
                    continue
                previous[key] = value
            payload = previous
        try:
            self.journal.write(result.operation_id, payload)
        except OSError as exc:
            # The core state may already be committed, but a non-durable terminal
            # record is itself a repair condition. The previous in-progress
            # journal remains discoverable by bootstrap.
            return replace(
                result,
                status=OperationStatus.REPAIR_REQUIRED,
                errors=result.errors
                + (
                    _error(
                        ErrorCode.STORAGE_REPAIR_REQUIRED,
                        f"操作终态日志持久化失败: {exc}",
                        recoverable=True,
                    ),
                ),
                repair_actions=tuple(
                    dict.fromkeys((*result.repair_actions, "repair_operation_journal"))
                ),
            )
        return result


def _terminal_record(
    record: dict[str, Any],
    status: OperationStatus,
    stage: StorageStage,
    *,
    message: Optional[str] = None,
    repairs: Iterable[str] = (),
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Build a terminal journal record preserving recorded paths/fields."""
    payload = dict(record)
    payload["status"] = status.value
    payload["stage"] = stage.value
    payload["updated_at"] = _utc_now()
    errors = list(record.get("errors") or [])
    if message:
        errors.append(
            _error(ErrorCode.STORAGE_REPAIR_REQUIRED, message, recoverable=True)
        )
    payload["errors"] = errors
    payload["repair_actions"] = list(
        dict.fromkeys((*(record.get("repair_actions") or []), *repairs))
    )
    if note:
        payload["recovery_note"] = note
    return payload


def _restore_quarantine_and_reject(
    record: dict[str, Any],
    markdown_store: MarkdownStore,
    quarantine_path: Any,
) -> Optional[dict[str, Any]]:
    """Deterministically restore a quarantined primary file; None if not provable."""
    if not isinstance(quarantine_path, str) or not quarantine_path:
        return None
    relative = record.get("relative_file_path")
    if not isinstance(relative, str) or not relative:
        return None
    identity = _recorded_file_identity(record, "quarantine") or _recorded_file_identity(
        record, "primary"
    )
    sha256 = _recorded_file_sha256(
        record, "quarantine"
    ) or _recorded_file_sha256(record, "primary")
    if identity is None or sha256 is None:
        # Legacy records without an operation-bound identity and content digest
        # are not authority
        # to move whatever currently occupies the quarantine path.
        return None
    quarantine = Path(quarantine_path)
    try:
        original = markdown_store.gateway.resolve(relative, must_exist=False)
        markdown_store.restore(
            QuarantinedVaultFile(
                original,
                quarantine,
                identity[0],
                identity[1],
                sha256,
            )
        )
    except Exception:
        return None
    return _terminal_record(
        record,
        OperationStatus.REJECTED,
        StorageStage.COMPLETED,
        note="崩溃恢复：删除未提交，隔离文件已恢复",
    )


def _recorded_file_identity(
    record: dict[str, Any], prefix: str
) -> tuple[int, int] | None:
    st_dev = record.get(f"{prefix}_st_dev")
    st_ino = record.get(f"{prefix}_st_ino")
    if (
        isinstance(st_dev, int)
        and not isinstance(st_dev, bool)
        and st_dev >= 0
        and isinstance(st_ino, int)
        and not isinstance(st_ino, bool)
        and st_ino > 0
    ):
        return st_dev, st_ino
    return None


def _recorded_file_sha256(record: dict[str, Any], prefix: str) -> str | None:
    value = record.get(f"{prefix}_sha256")
    return value if _valid_sha256(value) else None


def _resolve_interrupted_archive(
    record: dict[str, Any],
    markdown_store: MarkdownStore,
    sqlite_store: SQLiteStore,
) -> Optional[dict[str, Any]]:
    """Resolve an interrupted archive only where the plan+state proves the outcome."""
    stage = record.get("stage")
    relative = record.get("planned_relative_file_path") or record.get(
        "relative_file_path"
    )
    if (
        stage == StorageStage.PREPARING.value
        and record.get("journal_schema_version") in {1, 2, 3}
        and record.get("checkpoint") == "journal_created"
        and not relative
    ):
        return _terminal_record(
            record,
            OperationStatus.REJECTED,
            StorageStage.COMPLETED,
            note="崩溃恢复：仅创建操作日志，尚未规划或产生副作用",
        )
    if not isinstance(relative, str) or not relative:
        return None
    try:
        markdown_store.gateway.resolve(relative, must_exist=False)
    except Exception:
        return None

    file_exists = False
    try:
        existing_path = markdown_store.gateway.resolve(
            relative, must_exist=True, require_file=True
        )
        expected_identity = _recorded_file_identity(record, "primary")
        expected_sha256 = _recorded_file_sha256(record, "primary")
        if expected_identity is not None and expected_sha256 is not None:
            observed_identity, observed_sha256 = (
                markdown_store.gateway.file_fingerprint(existing_path)
            )
            if (
                observed_identity != expected_identity
                or observed_sha256 != expected_sha256
            ):
                return None
        elif stage != StorageStage.PREPARING.value:
            # A committed-primary checkpoint without identity and content
            # continuity cannot authorize automatic recovery of the current path
            # occupant.
            return None
        file_exists = True
    except FileNotFoundError:
        file_exists = False
    except Exception:
        return None

    operation_id = record.get("operation_id")
    projection_sha256 = record.get("projection_sha256")
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or not isinstance(projection_sha256, str)
        or len(projection_sha256) != 64
    ):
        return None
    probe = _probe_archive_commit(
        sqlite_store,
        operation_id=operation_id,
        relative_file_path=relative,
        projection_sha256=projection_sha256,
    )
    if probe.state == "ambiguous":
        return None
    if probe.state == "committed" and probe.knowledge_id is not None:
        record = dict(record)
        record["knowledge_id"] = probe.knowledge_id

    vector_required = record.get("vector_required", False)
    if not isinstance(vector_required, bool):
        return None

    if stage == StorageStage.PREPARING.value:
        if file_exists or probe.state == "committed":
            # A file or row exists although only the plan was journaled:
            # ambiguous (user fact or partial write) - fail closed.
            return None
        return _terminal_record(
            record,
            OperationStatus.REJECTED,
            StorageStage.COMPLETED,
            note="崩溃恢复：仅记录计划，未产生任何副作用",
        )

    if stage in {
        StorageStage.PRIMARY_COMMITTED.value,
        StorageStage.INDEX_COMMITTED.value,
    }:
        if file_exists and probe.state == "committed":
            # Markdown + SQLite provably committed.  Vector state is unknown;
            # DEGRADED is the honest terminal when vectors were required.
            if vector_required:
                return _terminal_record(
                    record,
                    OperationStatus.DEGRADED,
                    StorageStage.INDEX_COMMITTED,
                    message="崩溃恢复：核心存储已提交，向量终态未知",
                    repairs=("rebuild_vectors_for_entry",),
                )
            return _terminal_record(
                record,
                OperationStatus.READY,
                StorageStage.COMPLETED,
                note="崩溃恢复：核心存储已提交",
            )
        if not file_exists and probe.state == "absent":
            return _terminal_record(
                record,
                OperationStatus.REJECTED,
                StorageStage.COMPLETED,
                note="崩溃恢复：主文件与索引均不存在，操作未提交",
            )
        return None

    if stage == StorageStage.VECTOR_COMMITTED.value:
        if file_exists and probe.state == "committed":
            return _terminal_record(
                record,
                OperationStatus.READY,
                StorageStage.COMPLETED,
                note="崩溃恢复：全部提交完成",
            )
        return None

    return None


def _resolve_interrupted_delete(
    record: dict[str, Any],
    markdown_store: MarkdownStore,
    sqlite_store: SQLiteStore,
) -> Optional[dict[str, Any]]:
    """Resolve an interrupted delete only where the plan+state proves the outcome."""
    stage = record.get("stage")
    if (
        stage == StorageStage.PREPARING.value
        and record.get("journal_schema_version") in {1, 2, 3}
        and record.get("checkpoint") == "journal_created"
        and not record.get("relative_file_path")
    ):
        return _terminal_record(
            record,
            OperationStatus.REJECTED,
            StorageStage.COMPLETED,
            note="崩溃恢复：仅创建删除日志，尚未规划或产生副作用",
        )
    primary_missing = record.get("primary_missing", False)
    if not isinstance(primary_missing, bool):
        return None
    relative = record.get("relative_file_path")
    if not isinstance(relative, str) or not relative:
        return None
    quarantine_value = record.get("quarantine_path") or record.get(
        "planned_quarantine_path"
    )
    operation_id = record.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        return None
    try:
        expected_original = markdown_store.gateway.resolve(relative, must_exist=False)
    except Exception:
        return None
    expected_quarantine = (
        markdown_store.gateway.vault_dir
        / ".pkv-quarantine"
        / f"{operation_id}-{expected_original.name}"
    )

    def same_lexical_path(left: Any, right: Path) -> bool:
        if not isinstance(left, str) or not left:
            return False
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(os.fspath(right))
        )

    if quarantine_value and not same_lexical_path(quarantine_value, expected_quarantine):
        # A journal must never be able to restore/purge a different operation's
        # quarantined user fact.
        return None
    recorded_original = record.get("original_path")
    if recorded_original and not same_lexical_path(recorded_original, expected_original):
        return None
    knowledge_id = record.get("knowledge_id")
    projection_sha256 = record.get("projection_sha256")
    if (
        not _valid_positive_int(knowledge_id)
        or not isinstance(projection_sha256, str)
        or len(projection_sha256) != 64
    ):
        return None
    commit_probe = _probe_delete_commit(
        sqlite_store,
        operation_id=operation_id,
        knowledge_id=knowledge_id,
        relative_file_path=relative,
        projection_sha256=projection_sha256,
    )
    if commit_probe.state == "ambiguous":
        return None
    delete_committed = commit_probe.state == "committed"
    delete_uncommitted = commit_probe.state == "absent"

    vector_required = record.get("vector_required", False)
    if not isinstance(vector_required, bool):
        return None
    vector_repairs = (
        ("remove_stale_vectors_for_entry",) if vector_required else ()
    )

    def quarantine_exists() -> Optional[bool]:
        if not isinstance(quarantine_value, str) or not quarantine_value:
            return False
        try:
            resolved = markdown_store.gateway.resolve(
                Path(quarantine_value),
                must_exist=True,
                require_file=True,
                allow_internal=True,
            )
            expected_identity = _recorded_file_identity(
                record, "quarantine"
            ) or _recorded_file_identity(record, "primary")
            expected_sha256 = _recorded_file_sha256(
                record, "quarantine"
            ) or _recorded_file_sha256(record, "primary")
            if expected_identity is None or expected_sha256 is None:
                return None
            observed_identity, observed_sha256 = (
                markdown_store.gateway.file_fingerprint(
                    resolved, allow_internal=True
                )
            )
            if (
                observed_identity != expected_identity
                or observed_sha256 != expected_sha256
            ):
                return None
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return None

    def original_exists() -> Optional[bool]:
        try:
            resolved = markdown_store.gateway.resolve(
                relative, must_exist=True, require_file=True
            )
            if not primary_missing:
                expected_identity = _recorded_file_identity(record, "primary")
                expected_sha256 = _recorded_file_sha256(record, "primary")
                if expected_identity is None or expected_sha256 is None:
                    return None
                observed_identity, observed_sha256 = (
                    markdown_store.gateway.file_fingerprint(resolved)
                )
                if (
                    observed_identity != expected_identity
                    or observed_sha256 != expected_sha256
                ):
                    return None
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return None

    if stage == StorageStage.DELETE_PLANNED.value:
        q_exists = quarantine_exists()
        o_exists = original_exists()
        if q_exists is None or o_exists is None:
            return None
        if o_exists and not q_exists:
            if delete_uncommitted:
                return _terminal_record(
                    record,
                    OperationStatus.REJECTED,
                    StorageStage.COMPLETED,
                    note="崩溃恢复：删除未开始提交",
                )
            return None
        if not o_exists and q_exists:
            # The move happened but the quarantined journal was never written:
            # restore is provably safe only while the SQLite row still proves
            # the delete did not commit.
            if delete_committed:
                return None
            return _restore_quarantine_and_reject(record, markdown_store, quarantine_value)
        if primary_missing:
            # The plan recorded the primary as already missing; nothing moved.
            if delete_committed:
                return _terminal_record(
                    record,
                    OperationStatus.DEGRADED,
                    StorageStage.COMPLETED,
                    message="崩溃恢复：删除核心已提交（主文件本就缺失）",
                    repairs=("audit_missing_primary_file",),
                )
            return _terminal_record(
                record,
                OperationStatus.REJECTED,
                StorageStage.COMPLETED,
                note="崩溃恢复：删除未提交",
            )
        return None

    if stage == StorageStage.DELETE_QUARANTINED.value:
        q_exists = quarantine_exists()
        o_exists = original_exists()
        if q_exists is None or o_exists is None:
            return None
        if q_exists:
            if delete_uncommitted:
                return _restore_quarantine_and_reject(
                    record, markdown_store, quarantine_value
                )
            # SQLite committed; the quarantined file remains.  Do not delete
            # user facts automatically - degrade with an explicit purge repair.
            repairs = ["purge_committed_quarantine", *vector_repairs]
            message = "崩溃恢复：删除核心已提交，隔离文件待清理"
            if o_exists:
                message += "；原路径也存在，需审计重复/重建事实"
                repairs.append("audit_unexpected_primary_after_delete")
            return _terminal_record(
                record,
                OperationStatus.DEGRADED,
                StorageStage.COMPLETED,
                message=message,
                repairs=repairs,
            )
        if delete_committed:
            if primary_missing:
                return _terminal_record(
                    record,
                    OperationStatus.DEGRADED,
                    StorageStage.COMPLETED,
                    message="崩溃恢复：删除核心已提交（主文件本就缺失）",
                    repairs=("audit_missing_primary_file", *vector_repairs),
                )
            if o_exists:
                # Never fabricate DELETED while a primary user fact is still
                # present, even if the SQLite row and quarantine are gone.
                return _terminal_record(
                    record,
                    OperationStatus.DEGRADED,
                    StorageStage.COMPLETED,
                    message="崩溃恢复：SQLite 删除已提交，但 Markdown 主文件仍存在",
                    repairs=(
                        "audit_unexpected_primary_after_delete",
                        *vector_repairs,
                    ),
                )
            if vector_required:
                return _terminal_record(
                    record,
                    OperationStatus.DEGRADED,
                    StorageStage.COMPLETED,
                    message="崩溃恢复：删除核心已提交，向量清理终态未知",
                    repairs=vector_repairs,
                )
            return _terminal_record(
                record,
                OperationStatus.DELETED,
                StorageStage.COMPLETED,
                note="崩溃恢复：删除已提交",
            )
        if o_exists:
            # SQLite still contains the row and the primary has already been
            # restored; this is a provably compensated delete.
            return _terminal_record(
                record,
                OperationStatus.REJECTED,
                StorageStage.COMPLETED,
                note="崩溃恢复：删除未提交，主文件已恢复",
            )
        if primary_missing:
            # The plan recorded the primary as already missing; the SQLite
            # delete never committed and there is nothing to restore.
            return _terminal_record(
                record,
                OperationStatus.REJECTED,
                StorageStage.COMPLETED,
                note="崩溃恢复：删除未提交（主文件本就缺失）",
            )
        return None

    if stage == StorageStage.COMPENSATING.value:
        # repair_required from a failed compensation: restore only when provable.
        if "restore_quarantined_markdown" in (record.get("repair_actions") or []):
            if not delete_uncommitted:
                return None
            q_exists = quarantine_exists()
            if q_exists is not True:
                return None
            return _restore_quarantine_and_reject(
                record, markdown_store, quarantine_value
            )
        return None

    return None


def _resolve_interrupted_patch(
    record: dict[str, Any],
    markdown_store: MarkdownStore,
    sqlite_store: SQLiteStore,
) -> Optional[dict[str, Any]]:
    """Resolve an interrupted AI patch only from revision-bound evidence.

    A patch has one extra destructive boundary compared with archive: it first
    moves the old Markdown into the operation-derived quarantine location, then
    publishes a replacement.  The planned journal records the old file's exact
    identity before that move, and each later checkpoint records the new primary
    identity.  Recovery therefore restores or deletes only files that the
    journal can prove belong to this operation.
    """

    stage = record.get("stage")
    checkpoint = record.get("checkpoint")
    if checkpoint == "journal_created":
        if (
            stage == StorageStage.PREPARING.value
            and not record.get("relative_file_path")
            and not record.get("resulting_revision_sha256")
        ):
            return _terminal_record(
                record,
                OperationStatus.REJECTED,
                StorageStage.COMPLETED,
                note="崩溃恢复：仅创建 DerivationPatch 日志，尚未规划或产生副作用",
            )
        return None

    if checkpoint not in {
        "patch_planned",
        "patch_quarantined",
        "patch_primary_committed",
        "patch_index_committed",
        "patch_compensating",
    }:
        return None
    operation_id = record.get("operation_id")
    knowledge_id = record.get("knowledge_id")
    relative = record.get("relative_file_path")
    previous_revision = record.get("previous_revision_sha256")
    resulting_revision = record.get("resulting_revision_sha256")
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or not _valid_positive_int(knowledge_id)
        or not isinstance(relative, str)
        or not relative
        or not _valid_sha256(previous_revision)
        or not _valid_sha256(resulting_revision)
    ):
        return None
    previous_identity = _recorded_file_identity(record, "previous_primary")
    previous_sha256 = _recorded_file_sha256(record, "previous_primary")
    if previous_identity is None or previous_sha256 is None:
        return None

    try:
        original = markdown_store.gateway.resolve(relative, must_exist=False)
    except Exception:
        return None
    expected_quarantine = (
        markdown_store.gateway.vault_dir
        / ".pkv-quarantine"
        / f"{operation_id}-{original.name}"
    )

    def same_lexical_path(left: Any, right: Path) -> bool:
        return isinstance(left, str) and bool(left) and os.path.normcase(
            os.path.abspath(left)
        ) == os.path.normcase(os.path.abspath(os.fspath(right)))

    if not same_lexical_path(record.get("original_path"), original) or not same_lexical_path(
        record.get("planned_quarantine_path"), expected_quarantine
    ):
        return None
    recorded_quarantine = record.get("quarantine_path")
    if recorded_quarantine and not same_lexical_path(
        recorded_quarantine, expected_quarantine
    ):
        return None

    probe = _probe_patch_commit(
        sqlite_store,
        operation_id=operation_id,
        knowledge_id=knowledge_id,
        relative_file_path=relative,
        previous_revision_sha256=previous_revision,
        resulting_revision_sha256=resulting_revision,
    )
    if probe.state == "ambiguous":
        return None

    def path_exists() -> Optional[bool]:
        try:
            markdown_store.gateway.resolve(relative, must_exist=True, require_file=True)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return None

    def revision_matches(expected_revision: str) -> Optional[bool]:
        try:
            primary = markdown_store.gateway.resolve(
                relative, must_exist=True, require_file=True
            )
            entry = markdown_store.load(primary)
            chunks = sqlite_store.get_chunks_by_knowledge_id(knowledge_id)
            return (
                entry_projection_sha256(
                    entry,
                    relative,
                    [str(item["chunk_text"]) for item in chunks],
                )
                == expected_revision
            )
        except FileNotFoundError:
            return False
        except Exception:
            return None

    def exact_file_state(
        candidate: Path,
        identity: tuple[int, int] | None,
        sha256: str | None,
        *,
        allow_internal: bool = False,
    ) -> Optional[bool]:
        if identity is None or sha256 is None:
            return None
        try:
            resolved = markdown_store.gateway.resolve(
                candidate,
                must_exist=True,
                require_file=True,
                allow_internal=allow_internal,
            )
            observed_identity, observed_sha256 = markdown_store.gateway.file_fingerprint(
                resolved,
                allow_internal=allow_internal,
            )
            return observed_identity == identity and observed_sha256 == sha256
        except FileNotFoundError:
            return False
        except Exception:
            return None

    quarantine_identity = _recorded_file_identity(record, "quarantine")
    quarantine_sha256 = _recorded_file_sha256(record, "quarantine")
    if checkpoint == "patch_planned":
        # The plan itself owns the old primary identity.  A crash immediately
        # after the move still has enough evidence to restore it if the primary
        # path is empty.
        quarantine_identity = previous_identity
        quarantine_sha256 = previous_sha256
    quarantine_state = exact_file_state(
        expected_quarantine,
        quarantine_identity,
        quarantine_sha256,
        allow_internal=True,
    )
    primary_exists = path_exists()
    if primary_exists is None or quarantine_state is None:
        return None
    old_primary = revision_matches(previous_revision)
    new_primary = revision_matches(resulting_revision)
    if old_primary is None or new_primary is None:
        return None

    def reject_after_restore() -> Optional[dict[str, Any]]:
        try:
            markdown_store.gateway.restore(
                QuarantinedVaultFile(
                    original,
                    expected_quarantine,
                    previous_identity[0],
                    previous_identity[1],
                    previous_sha256,
                )
            )
        except Exception:
            return None
        return _terminal_record(
            record,
            OperationStatus.REJECTED,
            StorageStage.COMPLETED,
            note="崩溃恢复：DerivationPatch 未提交，旧 Markdown 已恢复",
        )

    def committed_terminal(
        status: OperationStatus,
        *,
        message: str | None = None,
        repairs: Iterable[str] = (),
        note: str | None = None,
    ) -> dict[str, Any]:
        terminal = _terminal_record(
            record,
            status,
            StorageStage.COMPLETED,
            message=message,
            repairs=repairs,
            note=note,
        )
        terminal["core_committed"] = True
        return terminal

    if checkpoint in {"patch_planned", "patch_quarantined"}:
        if probe.state == "absent":
            if quarantine_state and primary_exists is False:
                return reject_after_restore()
            if not quarantine_state and old_primary:
                return _terminal_record(
                    record,
                    OperationStatus.REJECTED,
                    StorageStage.COMPLETED,
                    note="崩溃恢复：DerivationPatch 未提交，旧 Markdown 与 SQLite 均未改变",
                )
            return None
        if probe.state == "committed" and not quarantine_state and new_primary:
            return committed_terminal(
                OperationStatus.READY,
                note="崩溃恢复：DerivationPatch 核心已提交",
            )
        return None

    published_identity = _recorded_file_identity(record, "primary")
    published_sha256 = _recorded_file_sha256(record, "primary")
    primary_state = exact_file_state(
        original,
        published_identity,
        published_sha256,
    )
    if primary_state is None:
        return None

    if probe.state == "committed":
        if not primary_state or not new_primary:
            return None
        if quarantine_state:
            # The committed old primary is retained as a repair artifact.  Do
            # not delete it at startup: even a path with a matching name is a
            # user fact unless a repair action explicitly performs the purge.
            return committed_terminal(
                OperationStatus.DEGRADED,
                message="崩溃恢复：DerivationPatch 核心已提交，旧 Markdown 隔离副本待清理",
                repairs=("purge_patch_quarantine",),
            )
        return committed_terminal(
            OperationStatus.READY,
            note="崩溃恢复：DerivationPatch 核心已提交",
        )

    # The SQLite predecessor still exists.  Compensate only the exact new
    # primary followed by the exact quarantined predecessor; any replacement or
    # content mismatch remains a blocking repair instead of deleting user data.
    if primary_state and quarantine_state:
        try:
            removed = markdown_store.gateway.delete_if_identity(
                original,
                expected_identity=published_identity,
                expected_sha256=published_sha256,
            )
            if not removed:
                return None
        except Exception:
            return None
        return reject_after_restore()
    if primary_exists is False and quarantine_state:
        return reject_after_restore()
    if not quarantine_state and old_primary:
        return _terminal_record(
            record,
            OperationStatus.REJECTED,
            StorageStage.COMPLETED,
            note="崩溃恢复：DerivationPatch 未提交，旧 Markdown 已恢复",
        )
    return None


def _resolve_interrupted(
    record: dict[str, Any],
    markdown_store: MarkdownStore,
    sqlite_store: SQLiteStore,
) -> Optional[dict[str, Any]]:
    action = record.get("action")
    if action == "archive":
        return _resolve_interrupted_archive(record, markdown_store, sqlite_store)
    if action == "delete":
        return _resolve_interrupted_delete(record, markdown_store, sqlite_store)
    if action == "apply_ai_patch":
        return _resolve_interrupted_patch(record, markdown_store, sqlite_store)
    return None


def recover_interrupted_operations(
    journal: StorageOperationJournal,
    markdown_store: MarkdownStore,
    sqlite_store: SQLiteStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministically resolve crash-boundary journal records at startup.

    Returns ``(usable_repair_records, blocking_records)``.  Provable states are
    terminated idempotently in the journal; ambiguous states are returned so
    the caller can fail closed with ``STORAGE_REPAIR_REQUIRED``.  User facts
    are never deleted and success is never fabricated.
    """
    usable: list[dict[str, Any]] = []
    blocking: list[dict[str, Any]] = []
    for record in journal.records():
        status = record.get("status")
        if status == OperationStatus.DEGRADED.value:
            # Ordinary terminal vector repairs stay startup-usable.
            usable.append(record)
            continue
        if status not in {"in_progress", OperationStatus.REPAIR_REQUIRED.value}:
            continue
        terminal = _resolve_interrupted(record, markdown_store, sqlite_store)
        if terminal is None:
            blocking.append(record)
            continue
        try:
            journal.write(record["operation_id"], terminal)
        except OSError:
            blocking.append(record)
            continue
        if terminal.get("status") == OperationStatus.DEGRADED.value:
            usable.append(terminal)
    return usable, blocking
