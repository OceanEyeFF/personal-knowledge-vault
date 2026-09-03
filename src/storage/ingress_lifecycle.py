"""Durable R4 Q0 ingress admission and private request spool.

Q0 is deliberately separate from the Q1′ content writer.  It records a small,
fenced request row while the data-root writer lease is held, then permits the
potentially slow crawler/parser to run without that lease.  Raw text, URLs, and
authorized file paths live only in the private spool; SQLite retains immutable
references, hashes, and recovery state.
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
from dataclasses import dataclass
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
from src.storage.sqlite_connection import connect_existing_sqlite


_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_TASK_ASSET_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_OWNER_FENCE_DIR_PATTERN = re.compile(r"[1-9][0-9]{0,18}\Z")
_TEMPORARY_IMAGE_SUFFIXES = frozenset(
    {".avif", ".bmp", ".gif", ".img", ".jfif", ".jpeg", ".jpg", ".png", ".webp"}
)
_SPOOL_SCHEMA_VERSION = 1
_STAGE = "r4_ingress_lifecycle"
_MAX_TASK_SPOOL_NODES = 256


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


class IngressKind(str, Enum):
    URL = "url"
    TEXT = "text"
    FILE = "file"


class IngressState(str, Enum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    PREPARED = "prepared"
    SUBMITTED = "submitted"
    RETRY_REQUIRED = "retry_required"
    REJECTED = "rejected"
    REPAIR_REQUIRED = "repair_required"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class IngressRequest:
    """Versioned Q0 input retained only in the private spool."""

    request_id: str
    kind: IngressKind
    source: str
    title_override: str
    provenance: Mapping[str, str]
    parser_version: str
    input_digest: str

    @classmethod
    def create(
        cls,
        kind: IngressKind,
        source: str,
        *,
        title_override: str = "",
        provenance: Mapping[str, str] | None = None,
        parser_version: str = "r4-q0-v1",
        request_id: str | None = None,
    ) -> "IngressRequest":
        values = cls._normalized_values(
            request_id or uuid.uuid4().hex,
            kind,
            source,
            title_override,
            provenance,
            parser_version,
        )
        return cls(*values, input_digest=_sha256(cls._digest_payload(*values)))

    @staticmethod
    def _normalized_values(
        request_id: object,
        kind: IngressKind,
        source: object,
        title_override: object,
        provenance: Mapping[str, str] | None,
        parser_version: object,
    ) -> tuple[str, IngressKind, str, str, dict[str, str], str]:
        identifier = _require_id(request_id, label="request_id")
        if not isinstance(kind, IngressKind):
            raise TypeError("ingress kind 无效")
        if not isinstance(source, str) or not source:
            raise ValueError("ingress source 不能为空")
        if not isinstance(title_override, str):
            raise ValueError("title_override 必须是字符串")
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
        return (
            identifier,
            kind,
            source,
            title_override.strip(),
            normalized_provenance,
            parser_version,
        )

    @staticmethod
    def _digest_payload(
        request_id: str,
        kind: IngressKind,
        source: str,
        title_override: str,
        provenance: Mapping[str, str],
        parser_version: str,
    ) -> dict[str, object]:
        del request_id
        return {
            "schema_version": _SPOOL_SCHEMA_VERSION,
            "kind": kind.value,
            "source": source,
            "title_override": title_override,
            "provenance": dict(provenance),
            "parser_version": parser_version,
        }

    def to_payload(self) -> dict[str, object]:
        values = self._normalized_values(
            self.request_id,
            self.kind,
            self.source,
            self.title_override,
            self.provenance,
            self.parser_version,
        )
        expected = _sha256(self._digest_payload(*values))
        if self.input_digest != expected:
            raise ValueError("IngressRequest input_digest 不匹配")
        return {
            "schema_version": _SPOOL_SCHEMA_VERSION,
            "request_id": self.request_id,
            "kind": self.kind.value,
            "source": self.source,
            "title_override": self.title_override,
            "provenance": dict(self.provenance),
            "parser_version": self.parser_version,
            "input_digest": self.input_digest,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "IngressRequest":
        if not isinstance(payload, Mapping):
            raise ValueError("IngressRequest spool 不是对象")
        if set(payload) != {
            "schema_version",
            "request_id",
            "kind",
            "source",
            "title_override",
            "provenance",
            "parser_version",
            "input_digest",
        }:
            raise ValueError("IngressRequest spool 字段无效")
        if payload.get("schema_version") != _SPOOL_SCHEMA_VERSION:
            raise ValueError("IngressRequest spool schema_version 不受支持")
        try:
            kind = IngressKind(payload.get("kind"))
        except (TypeError, ValueError) as exc:
            raise ValueError("ingress kind 无效") from exc
        values = cls._normalized_values(
            payload.get("request_id"),
            kind,
            payload.get("source"),
            payload.get("title_override"),
            payload.get("provenance"),
            payload.get("parser_version"),
        )
        digest = _require_sha256(payload.get("input_digest"), label="input_digest")
        if digest != _sha256(cls._digest_payload(*values)):
            raise ValueError("IngressRequest input_digest 不匹配")
        return cls(*values, input_digest=digest)


@dataclass(frozen=True)
class IngressRequestReference:
    request_id: str
    payload_sha256: str


@dataclass(frozen=True)
class PreparedReference:
    prepared_id: str
    payload_sha256: str


class IngressRequestSpool:
    """Safe immutable Q0 request files below private runtime state."""

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._root = Path(layout.runtime_state_dir) / "r4" / "ingress"

    def _root_for_write(self) -> Path:
        require_active_data_root_writer(self._layout, owner="r4_ingress_spool")
        return ensure_safe_directory(self._root, label="R4 ingress spool")

    def _root_for_read(self) -> Path:
        return validate_directory_components(self._root, label="R4 ingress spool")

    @staticmethod
    def _path(root: Path, request_id: str) -> Path:
        return root / f"{_require_id(request_id, label='request_id')}.json"

    def write(self, request: IngressRequest) -> IngressRequestReference:
        if not isinstance(request, IngressRequest):
            raise TypeError("request 必须是 IngressRequest")
        root = self._root_for_write()
        target = self._path(root, request.request_id)
        encoded = _canonical_bytes(request.to_payload()) + b"\n"
        digest = hashlib.sha256(encoded).hexdigest()
        if os.path.lexists(target):
            existing = self.read(IngressRequestReference(request.request_id, digest))
            if existing.to_payload() != request.to_payload():
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "同一 ingress request identity 的私有 payload 已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return IngressRequestReference(request.request_id, digest)
        atomic_publish_file(target, label="R4 ingress request", data=encoded)
        return IngressRequestReference(request.request_id, digest)

    def read(self, reference: IngressRequestReference) -> IngressRequest:
        if not isinstance(reference, IngressRequestReference):
            raise TypeError("reference 必须是 IngressRequestReference")
        request_id = _require_id(reference.request_id, label="request_id")
        expected = _require_sha256(reference.payload_sha256, label="payload_sha256")
        target = self._path(self._root_for_read(), request_id)
        try:
            with open_user_file_nofollow(
                target,
                "rb",
                label="R4 ingress request",
            ) as handle:
                encoded = handle.read()
        except (OSError, PKVRuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "ingress request 无法安全读取。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        if hashlib.sha256(encoded).hexdigest() != expected:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "ingress request 摘要不匹配。",
                stage=_STAGE,
                recoverable=True,
            )
        try:
            request = IngressRequest.from_payload(json.loads(encoded.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "ingress request 不可验证。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        if request.request_id != request_id:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "ingress request identity 不匹配。",
                stage=_STAGE,
                recoverable=True,
            )
        return request

    def discard(self, reference: IngressRequestReference) -> bool:
        """Best-effort cleanup after a verified PreparedDocument takes over.

        Q0 input is private recovery evidence until ``prepared`` is durable.  A
        mismatched or unsafe leaf is intentionally retained and never unlinked;
        callers can continue from the already-durable next state without treating
        privacy cleanup as a content-commit failure.
        """

        if not isinstance(reference, IngressRequestReference):
            raise TypeError("reference 必须是 IngressRequestReference")
        request_id = _require_id(reference.request_id, label="request_id")
        expected_digest = _require_sha256(reference.payload_sha256, label="payload_sha256")
        target = self._path(self._root_for_write(), request_id)
        try:
            before = os.lstat(target)
            if not stat.S_ISREG(before.st_mode):
                return False
            with open_user_file_nofollow(
                target,
                "rb",
                label="R4 ingress request",
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
class IngressTaskSpoolGrant:
    """Narrow Q0 capability for one claimed task-attempt's temporary assets.

    The grant is deliberately not a general data-root writer.  It can publish
    only regular temporary-image leaves below ``<task_id>/<owner_fence>/assets``
    and rechecks the durable Q0 claim at the atomic-publication boundary.  A
    stale worker can therefore never write into a newer attempt's workspace.
    """

    _spool: "IngressTaskSpool"
    task_id: str
    claim_token: str
    owner_fence: int

    def validate(self) -> None:
        """Fail closed when the fenced Q0 claim is no longer active."""

        self._spool._verify_active_claim(
            self.task_id,
            self.claim_token,
            self.owner_fence,
        )

    def write_image_asset(self, name: str, data: bytes) -> Path:
        """Atomically publish one temporary image without a root writer lease."""

        return self._spool._write_image_asset(
            self.task_id,
            self.claim_token,
            self.owner_fence,
            name,
            data,
        )


class IngressTaskSpool:
    """Task-private Q0 temporary workspace under the private ingress spool.

    Request payloads remain immutable ``<request_id>.json`` leaves.  Slow Q0
    work may additionally need transient local artifacts (currently processor
    image downloads).  Those artifacts use the opaque durable task identity,
    never a content hash or source URL, and are scoped by the claim fence:

    ``runtime/r4/ingress/<task_id>/<owner_fence>/assets/<name>``

    The task/attempt directories are created while the short root writer lease
    is held.  The returned grant is the sole narrow exception that permits an
    already-claimed Q0 worker to publish an asset while parsing without that
    root lease.  It stores no raw webpage body; callers must keep fetched HTML
    in memory and pass only explicitly supported temporary images here.  File
    attachment retention is deliberately a separate future contract.
    """

    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._root = Path(layout.runtime_state_dir) / "r4" / "ingress"

    @staticmethod
    def _require_owner_fence(value: object) -> int:
        if type(value) is not int or value <= 0:
            raise ValueError("owner_fence 必须是正整数")
        return value

    @staticmethod
    def _asset_name(value: object) -> str:
        if not isinstance(value, str) or _TASK_ASSET_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("Q0 临时 asset 名称无效")
        return value

    @classmethod
    def _image_asset_name(cls, value: object) -> str:
        name = cls._asset_name(value)
        if Path(name).suffix.lower() not in _TEMPORARY_IMAGE_SUFFIXES:
            raise ValueError("Q0 当前只允许临时图片 asset")
        return name

    def _root_for_write(self) -> Path:
        require_active_data_root_writer(self._layout, owner="r4_ingress_task_spool")
        self._layout.validate_user_directory(
            self._root,
            label="R4 ingress task spool",
        )
        return ensure_safe_directory(self._root, label="R4 ingress task spool")

    def _task_dir(self, root: Path, task_id: str) -> Path:
        return root / _require_id(task_id, label="task_id")

    def _attempt_dir(self, root: Path, task_id: str, owner_fence: int) -> Path:
        return self._task_dir(root, task_id) / str(self._require_owner_fence(owner_fence))

    def _assets_dir(self, root: Path, task_id: str, owner_fence: int) -> Path:
        return self._attempt_dir(root, task_id, owner_fence) / "assets"

    def _ensure_task_dir(self, path: Path, *, label: str) -> Path:
        self._layout.validate_user_directory(path, label=label)
        return ensure_safe_directory(path, label=label)

    def ensure_task(self, task_id: str) -> Path:
        """Reserve the opaque task workspace during short Q0 admission."""

        root = self._root_for_write()
        return self._ensure_task_dir(
            self._task_dir(root, task_id),
            label="R4 ingress task workspace",
        )

    def _verify_active_claim(
        self,
        task_id: str,
        claim_token: str,
        owner_fence: int,
    ) -> None:
        IngressTaskStore(self._layout).require_active_claim(
            task_id,
            claim_token=claim_token,
            owner_fence=owner_fence,
        )

    def prepare_attempt(self, task: "IngressTask") -> IngressTaskSpoolGrant:
        """Create the claim-fenced assets directory while the root lease is held."""

        if not isinstance(task, IngressTask):
            raise TypeError("task 必须是 IngressTask")
        if task.state is not IngressState.PROCESSING or task.claim_token is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "只有当前 processing Q0 task 可以取得临时工作区。",
                stage=_STAGE,
                recoverable=True,
            )
        self._verify_active_claim(task.task_id, task.claim_token, task.owner_fence)
        root = self._root_for_write()
        # An expired worker cannot publish after its claim has been replaced.
        # Reclaiming its bounded per-fence directory here keeps retries from
        # accumulating enough otherwise-valid image leaves to defeat the task
        # cleanup node limit after a later Q1′ core commit.
        self._discard_stale_attempts(
            root,
            task.task_id,
            keep_from_owner_fence=task.owner_fence,
        )
        self._ensure_task_dir(
            self._assets_dir(root, task.task_id, task.owner_fence),
            label="R4 ingress task attempt assets",
        )
        return IngressTaskSpoolGrant(
            self,
            task.task_id,
            task.claim_token,
            task.owner_fence,
        )

    def _write_image_asset(
        self,
        task_id: str,
        claim_token: str,
        owner_fence: int,
        name: str,
        data: bytes,
    ) -> Path:
        if type(data) is not bytes:
            raise TypeError("Q0 临时 asset 内容必须是 bytes")
        task_id = _require_id(task_id, label="task_id")
        claim_token = _require_id(claim_token, label="claim_token")
        owner_fence = self._require_owner_fence(owner_fence)
        name = self._image_asset_name(name)
        self._verify_active_claim(task_id, claim_token, owner_fence)
        root = validate_directory_components(self._root, label="R4 ingress task spool")
        assets_dir = self._assets_dir(root, task_id, owner_fence)
        self._layout.validate_user_directory(
            assets_dir,
            label="R4 ingress task attempt assets",
            allow_missing=False,
        )
        target = assets_dir / name
        self._layout.atomic_publish_user_file(
            target,
            label="R4 ingress temporary asset",
            data=data,
            # The claim must still be live at publication, rather than merely
            # at the beginning of an arbitrarily slow asset download.
            pre_replace=lambda: self._verify_active_claim(
                task_id,
                claim_token,
                owner_fence,
            ),
        )
        return target

    @staticmethod
    def _is_link_or_reparse(info: os.stat_result) -> bool:
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)

    @staticmethod
    def _same_identity(before: os.stat_result, after: os.stat_result) -> bool:
        return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)

    def _snapshot_tree(
        self,
        root: Path,
    ) -> list[tuple[Path, os.stat_result]] | None:
        """Capture a bounded, regular-file-only tree before best-effort cleanup."""

        pending = [root]
        snapshots: list[tuple[Path, os.stat_result]] = []
        while pending:
            path = pending.pop()
            try:
                info = os.lstat(path)
            except OSError:
                return None
            if self._is_link_or_reparse(info):
                return None
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    return None
                snapshots.append((path, info))
                if len(snapshots) > _MAX_TASK_SPOOL_NODES:
                    return None
                continue
            if not stat.S_ISDIR(info.st_mode):
                return None
            snapshots.append((path, info))
            if len(snapshots) > _MAX_TASK_SPOOL_NODES:
                return None
            try:
                with os.scandir(path) as entries:
                    for entry in entries:
                        pending.append(path / entry.name)
            except OSError:
                return None
        return snapshots

    def _discard_tree(self, target: Path, *, label: str) -> bool:
        """Best-effort no-follow cleanup for one bounded, owned tree.

        The caller already owns the root writer lease.  This helper deliberately
        treats every malformed or concurrently replaced path as retained repair
        material rather than deleting it by name.
        """

        if not os.path.lexists(target):
            return True
        try:
            self._layout.validate_user_directory(
                target,
                label=label,
                allow_missing=False,
            )
        except PKVRuntimeError:
            return False
        snapshots = self._snapshot_tree(target)
        if snapshots is None:
            return False
        # Files first, then the deepest directories.  Every identity is
        # rechecked immediately before mutation; a concurrent replacement is
        # preserved rather than deleted by name.
        ordered = sorted(
            snapshots,
            key=lambda item: (stat.S_ISDIR(item[1].st_mode), -len(item[0].parts)),
        )
        try:
            for path, before in ordered:
                current = os.lstat(path)
                if not self._same_identity(before, current):
                    return False
                if stat.S_ISREG(before.st_mode):
                    path.unlink()
                else:
                    path.rmdir()
        except OSError:
            return False
        return not os.path.lexists(target)

    def _discard_stale_attempts(
        self,
        root: Path,
        task_id: str,
        *,
        keep_from_owner_fence: int | None = None,
    ) -> bool:
        """Reclaim lower, fenced attempts without traversing unknown children.

        A newer claimant owns a higher durable fence.  It is safe to attempt
        cleanup of lower attempts after the claim transition, because grants
        from those attempts revalidate their task/token/fence at publication.
        Unknown, malformed, or newer directory names are left untouched and
        make this best-effort sweep report failure rather than being deleted.
        """

        task_dir = self._task_dir(root, task_id)
        if not os.path.lexists(task_dir):
            return True
        try:
            info = os.lstat(task_dir)
            if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                return False
            with os.scandir(task_dir) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError:
            return False

        complete = True
        for name in names:
            if _OWNER_FENCE_DIR_PATTERN.fullmatch(name) is None:
                complete = False
                continue
            owner_fence = int(name)
            if (
                keep_from_owner_fence is not None
                and owner_fence >= keep_from_owner_fence
            ):
                continue
            if not self._discard_tree(
                task_dir / name,
                label="R4 ingress stale task attempt",
            ):
                complete = False
        return complete

    def discard_task(self, task_id: str) -> bool:
        """Safely release all transient assets for a completed/rejected Q0 task.

        A malformed/replaced tree is intentionally retained for repair rather
        than recursively deleting an unknown path.  This method is idempotent:
        an already-released workspace counts as successfully released.
        """

        root = self._root_for_write()
        target = self._task_dir(root, task_id)
        if self._discard_tree(target, label="R4 ingress task workspace"):
            return True
        # The whole task is bounded in ordinary operation.  If a process was
        # interrupted through many retries before this hardening existed, first
        # reclaim each independently bounded fence subtree, then retry the
        # task-level cleanup.  Unknown or unsafe children remain fail-closed.
        self._discard_stale_attempts(root, task_id)
        return self._discard_tree(target, label="R4 ingress task workspace")


@dataclass(frozen=True)
class IngressTask:
    task_id: str
    operation_id: str
    kind: IngressKind
    request_ref: str
    request_sha256: str
    state: IngressState
    attempt_count: int
    claim_token: str | None = None
    claimed_until: str | None = None
    owner_fence: int = 0
    not_before: str | None = None
    prepared_ref: str | None = None
    prepared_sha256: str | None = None
    last_error_code: str | None = None

    @property
    def request_reference(self) -> IngressRequestReference:
        return IngressRequestReference(self.request_ref, self.request_sha256)

    @property
    def prepared_reference(self) -> PreparedReference | None:
        if self.prepared_ref is None or self.prepared_sha256 is None:
            return None
        return PreparedReference(self.prepared_ref, self.prepared_sha256)


def _task_from_row(row: sqlite3.Row) -> IngressTask:
    try:
        attempts = row["attempt_count"]
        fence = row["owner_fence"]
        if type(attempts) is not int or attempts < 0:
            raise ValueError("attempt_count")
        if type(fence) is not int or fence < 0:
            raise ValueError("owner_fence")
        prepared_ref = row["prepared_ref"]
        prepared_sha256 = row["prepared_sha256"]
        if (prepared_ref is None) != (prepared_sha256 is None):
            raise ValueError("prepared reference incomplete")
        return IngressTask(
            task_id=_require_id(row["task_id"], label="task_id"),
            operation_id=_require_id(row["operation_id"], label="operation_id"),
            kind=IngressKind(row["request_kind"]),
            request_ref=_require_id(row["request_ref"], label="request_ref"),
            request_sha256=_require_sha256(row["request_sha256"], label="request_sha256"),
            state=IngressState(row["state"]),
            attempt_count=attempts,
            claim_token=(
                _require_id(row["claim_token"], label="claim_token")
                if row["claim_token"] is not None
                else None
            ),
            claimed_until=(str(row["claimed_until"]) if row["claimed_until"] is not None else None),
            owner_fence=fence,
            not_before=(str(row["not_before"]) if row["not_before"] is not None else None),
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
            last_error_code=_require_error_code(row["last_error_code"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "Q0 ingress task 记录不可验证。",
            stage=_STAGE,
            recoverable=True,
        ) from exc


class IngressTaskStore:
    """SQLite state machine for Q0 admission, lease claims, and handoff prep."""

    _COLUMNS = """
        task_id, operation_id, request_kind, request_ref, request_sha256,
        prepared_ref, prepared_sha256, state, claim_token, claimed_until,
        owner_fence, attempt_count, not_before, last_error_code
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
                "Q0 ingress ledger 无法以只读方式读取。",
                stage=_STAGE,
                recoverable=True,
            ) from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        require_active_data_root_writer(self._layout, owner="r4_ingress_lifecycle")
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
                "Q0 ingress ledger 无法持久化。",
                stage=_STAGE,
                recoverable=True,
            ) from exc

    @classmethod
    def _load(cls, connection: sqlite3.Connection, task_id: str) -> IngressTask:
        row = connection.execute(
            f"SELECT {cls._COLUMNS} FROM ingress_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q0 ingress task 不存在或已变化。",
                stage=_STAGE,
                recoverable=True,
            )
        return _task_from_row(row)

    @classmethod
    def _load_by_operation(
        cls, connection: sqlite3.Connection, operation_id: str
    ) -> IngressTask | None:
        row = connection.execute(
            f"SELECT {cls._COLUMNS} FROM ingress_tasks WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return _task_from_row(row) if row is not None else None

    def enqueue(
        self,
        request: IngressRequest,
        reference: IngressRequestReference,
        *,
        operation_id: str | None = None,
    ) -> IngressTask:
        if not isinstance(request, IngressRequest) or not isinstance(
            reference, IngressRequestReference
        ):
            raise TypeError("request/reference 必须是 IngressRequest 及其引用")
        if reference.request_id != request.request_id:
            raise ValueError("ingress request 引用与 payload identity 不匹配")
        request_ref = _require_id(reference.request_id, label="request_ref")
        request_sha256 = _require_sha256(reference.payload_sha256, label="request_sha256")
        op_id = _require_id(operation_id or uuid.uuid4().hex, label="operation_id")
        with self._write_transaction() as connection:
            existing = self._load_by_operation(connection, op_id)
            if existing is not None:
                if (
                    existing.kind is request.kind
                    and existing.request_ref == request_ref
                    and existing.request_sha256 == request_sha256
                ):
                    return existing
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "同一 operation_id 的 Q0 ingress 输入已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            task_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO ingress_tasks(
                    task_id, operation_id, request_kind, request_ref,
                    request_sha256, state
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    op_id,
                    request.kind.value,
                    request_ref,
                    request_sha256,
                    IngressState.ACCEPTED.value,
                ),
            )
            return self._load(connection, task_id)

    def get_task(self, task_id: str) -> IngressTask | None:
        task_id = _require_id(task_id, label="task_id")
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {self._COLUMNS} FROM ingress_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row is not None else None

    def get_by_operation(self, operation_id: str) -> IngressTask | None:
        operation_id = _require_id(operation_id, label="operation_id")
        with self._read_connection() as connection:
            return self._load_by_operation(connection, operation_id)

    def require_active_claim(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
    ) -> IngressTask:
        """Return one live Q0 claim or fail closed without taking a root lease.

        This read-only predicate is the authority behind the deliberately narrow
        task-spool grant.  It does not authorize metadata, Vault, vector, log,
        or configuration writes; it merely proves that a worker still owns its
        own pre-created temporary attempt directory at the moment it publishes
        an asset.
        """

        task_id = _require_id(task_id, label="task_id")
        token = _require_id(claim_token, label="claim_token")
        if type(owner_fence) is not int or owner_fence <= 0:
            raise ValueError("owner_fence 必须是正整数")
        with self._read_connection() as connection:
            row = connection.execute(
                f"SELECT {self._COLUMNS} FROM ingress_tasks "
                "WHERE task_id = ? AND state = ? AND claim_token = ? "
                "AND owner_fence = ? AND claimed_until > CURRENT_TIMESTAMP",
                (
                    task_id,
                    IngressState.PROCESSING.value,
                    token,
                    owner_fence,
                ),
            ).fetchone()
        if row is None:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q0 ingress 临时工作区 claim 已失效。",
                stage=_STAGE,
                recoverable=True,
            )
        return _task_from_row(row)

    def list_submitted(self, *, limit: int = 8) -> tuple[IngressTask, ...]:
        """Return a bounded, deterministic slice of Q0 tasks handed to Q1′."""

        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须是正整数")
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT {self._COLUMNS} FROM ingress_tasks WHERE state = ? "
                "ORDER BY created_at ASC, task_id ASC LIMIT ?",
                (IngressState.SUBMITTED.value, limit),
            ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def list_recoverable(self) -> tuple[IngressTask, ...]:
        states = (
            IngressState.ACCEPTED.value,
            IngressState.PROCESSING.value,
            IngressState.PREPARED.value,
            IngressState.RETRY_REQUIRED.value,
        )
        placeholders = ", ".join("?" for _ in states)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT {self._COLUMNS} FROM ingress_tasks "
                f"WHERE state IN ({placeholders}) ORDER BY created_at ASC, task_id ASC",
                states,
            ).fetchall()
        return tuple(_task_from_row(row) for row in rows)

    def recover_expired_claims(self) -> int:
        """Return only expired Q0 claims to retry; live claims stay fenced."""

        with self._write_transaction() as connection:
            return connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE state = ?
                  AND (claimed_until IS NULL OR claimed_until <= CURRENT_TIMESTAMP)
                """,
                (
                    IngressState.RETRY_REQUIRED.value,
                    "worker_restarted",
                    IngressState.PROCESSING.value,
                ),
            ).rowcount

    def claim_next(self, *, lease_seconds: int = 120) -> IngressTask | None:
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须是 1..3600 的整数")
        modifier = f"+{lease_seconds} seconds"
        with self._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT task_id FROM ingress_tasks
                WHERE state IN (?, ?)
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                ORDER BY created_at ASC, task_id ASC
                LIMIT 1
                """,
                (IngressState.ACCEPTED.value, IngressState.RETRY_REQUIRED.value),
            ).fetchone()
            if row is None:
                return None
            task_id = _require_id(row["task_id"], label="task_id")
            token = uuid.uuid4().hex
            changed = connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, claim_token = ?,
                    claimed_until = datetime('now', ?),
                    owner_fence = owner_fence + 1,
                    attempt_count = attempt_count + 1,
                    last_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN (?, ?)
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                """,
                (
                    IngressState.PROCESSING.value,
                    token,
                    modifier,
                    task_id,
                    IngressState.ACCEPTED.value,
                    IngressState.RETRY_REQUIRED.value,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q0 ingress task 在 claim 前已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load(connection, task_id)

    def claim_task(self, task_id: str, *, lease_seconds: int = 120) -> IngressTask | None:
        """Fence one identified recoverable Q0 task without claiming its neighbour.

        Foreground status/continuation calls are addressed to a concrete ingress
        identity.  They must never claim an older FIFO row and then return before
        running its crawler, because that would leave a live, invisible lease
        until timeout.  Bulk recovery retains ``claim_next`` ordering.
        """

        task_id = _require_id(task_id, label="task_id")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds 必须是 1..3600 的整数")
        modifier = f"+{lease_seconds} seconds"
        with self._write_transaction() as connection:
            token = uuid.uuid4().hex
            changed = connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, claim_token = ?,
                    claimed_until = datetime('now', ?),
                    owner_fence = owner_fence + 1,
                    attempt_count = attempt_count + 1,
                    last_error_code = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state IN (?, ?)
                  AND (not_before IS NULL OR not_before <= CURRENT_TIMESTAMP)
                """,
                (
                    IngressState.PROCESSING.value,
                    token,
                    modifier,
                    task_id,
                    IngressState.ACCEPTED.value,
                    IngressState.RETRY_REQUIRED.value,
                ),
            ).rowcount
            if changed == 0:
                return None
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.REPAIR_REQUIRED,
                    "Q0 ingress task claim 影响了异常行数。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load(connection, task_id)

    @staticmethod
    def _validated_claim(task: IngressTask, claim_token: str, owner_fence: int) -> tuple[str, int]:
        token = _require_id(claim_token, label="claim_token")
        if type(owner_fence) is not int or owner_fence <= 0:
            raise ValueError("owner_fence 必须是正整数")
        if task.claim_token != token or task.owner_fence != owner_fence:
            raise PKVRuntimeError(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Q0 ingress claim 已失效。",
                stage=_STAGE,
                recoverable=True,
            )
        return token, owner_fence

    def mark_prepared(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        prepared_reference: PreparedReference,
    ) -> IngressTask:
        task_id = _require_id(task_id, label="task_id")
        reference = PreparedReference(
            _require_id(prepared_reference.prepared_id, label="prepared_ref"),
            _require_sha256(prepared_reference.payload_sha256, label="prepared_sha256"),
        )
        with self._write_transaction() as connection:
            task = self._load(connection, task_id)
            token, fence = self._validated_claim(task, claim_token, owner_fence)
            changed = connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, prepared_ref = ?, prepared_sha256 = ?,
                    claim_token = NULL, claimed_until = NULL, last_error_code = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    IngressState.PREPARED.value,
                    reference.prepared_id,
                    reference.payload_sha256,
                    task_id,
                    IngressState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q0 ingress prepared 写入已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load(connection, task_id)

    def mark_retry(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        error_code: str,
        delay_seconds: int = 0,
    ) -> IngressTask:
        task_id = _require_id(task_id, label="task_id")
        code = _require_error_code(error_code)
        if code is None:
            raise ValueError("error_code 不能为空")
        if type(delay_seconds) is not int or not 0 <= delay_seconds <= 86_400:
            raise ValueError("delay_seconds 必须是 0..86400 的整数")
        modifier = f"+{delay_seconds} seconds"
        with self._write_transaction() as connection:
            task = self._load(connection, task_id)
            token, fence = self._validated_claim(task, claim_token, owner_fence)
            changed = connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    not_before = datetime('now', ?), last_error_code = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    IngressState.RETRY_REQUIRED.value,
                    modifier,
                    code,
                    task_id,
                    IngressState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q0 ingress retry 写入已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load(connection, task_id)

    def mark_rejected(
        self,
        task_id: str,
        *,
        claim_token: str,
        owner_fence: int,
        error_code: str,
    ) -> IngressTask:
        task_id = _require_id(task_id, label="task_id")
        code = _require_error_code(error_code)
        if code is None:
            raise ValueError("error_code 不能为空")
        with self._write_transaction() as connection:
            task = self._load(connection, task_id)
            token, fence = self._validated_claim(task, claim_token, owner_fence)
            changed = connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, claim_token = NULL, claimed_until = NULL,
                    last_error_code = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ? AND claim_token = ? AND owner_fence = ?
                  AND claimed_until > CURRENT_TIMESTAMP
                """,
                (
                    IngressState.REJECTED.value,
                    code,
                    task_id,
                    IngressState.PROCESSING.value,
                    token,
                    fence,
                ),
            ).rowcount
            if changed != 1:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "Q0 ingress rejected 写入已过期。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return self._load(connection, task_id)

    def mark_submitted(self, task_id: str) -> IngressTask:
        task_id = _require_id(task_id, label="task_id")
        with self._write_transaction() as connection:
            task = self._load(connection, task_id)
            if task.state is IngressState.SUBMITTED:
                return task
            if task.state is not IngressState.PREPARED or task.prepared_reference is None:
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "尚未得到 PreparedDocument，不能完成 Q0 提交。",
                    stage=_STAGE,
                    recoverable=True,
                )
            connection.execute(
                """
                UPDATE ingress_tasks
                SET state = ?, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ? AND state = ?
                """,
                (IngressState.SUBMITTED.value, task_id, IngressState.PREPARED.value),
            )
            return self._load(connection, task_id)


__all__ = [
    "IngressKind",
    "IngressRequest",
    "IngressRequestReference",
    "IngressRequestSpool",
    "IngressState",
    "IngressTask",
    "IngressTaskSpool",
    "IngressTaskSpoolGrant",
    "IngressTaskStore",
    "PreparedReference",
]
