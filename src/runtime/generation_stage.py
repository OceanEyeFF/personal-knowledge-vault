"""Fenced private staging capability for one Embedding generation build.

R3 normally requires a root writer lease for every persistent data-root write.
Embedding rebuilds have one deliberately narrow exception: after a confirmed
operation has allocated a private staging directory under that lease, Provider
work may write only that operation's unreferenced vector stage while the root
lease is released.  Publication, audit, runtime snapshot changes, and every
other data-root path still require the ordinary root lease.

The capability is represented by a durable, secret-free marker inside the
private stage.  It is never part of a public API and cannot authorize a final
generation directory or any user content path.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer


_MARKER_NAME = ".generation-stage-grant.json"
_SCHEMA_VERSION = 1
_DEFAULT_TTL_SECONDS = 15 * 60
_OPERATION_ID_PATTERN = re.compile(r"embedding-rebuild-[0-9a-f]{32}\Z")
# ``GenerationStageGrant`` is intentionally a concrete, issued capability, not
# a structural protocol.  The object token is private to this module and is
# attached only by ``allocate`` after the root-writer lease has created and
# persisted the matching marker.  This is not a security boundary against code
# already running inside the PKV process; it prevents an unrelated product
# object from gaining the lease exception merely by defining a method with the
# same name.
_ISSUED_CAPABILITY = object()


def _safe_error(code: ErrorCode, message: str, *, stage: str) -> PKVRuntimeError:
    return PKVRuntimeError(code, message, stage=stage, recoverable=True)


def _now_millis() -> int:
    return int(time.time() * 1000)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class GenerationStageGrant:
    """One private, time-bound capability for an unreferenced vector stage.

    The grant is allocated only under the active data-root writer lease.  A
    later VectorStore may use it outside that lease, but it must revalidate the
    durable marker before each mutable stage operation.  The opaque fence is
    intentionally not serialized through any public DTO.
    """

    layout: Any = field(repr=False, compare=False)
    stage_dir: Path
    operation_id: str
    owner_fence: str = field(repr=False)
    plan_id: str
    source_revision: str
    expires_at_ms: int
    _clock: Callable[[], int] = field(
        default=_now_millis,
        repr=False,
        compare=False,
    )
    _capability: object = field(init=False, repr=False, compare=False)

    @property
    def marker_path(self) -> Path:
        return self.stage_dir / _MARKER_NAME

    @classmethod
    def allocate(
        cls,
        config: Any,
        *,
        plan_id: str,
        source_revision: str,
        operation_id: str | None = None,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], int] | None = None,
    ) -> "GenerationStageGrant":
        """Allocate one stage only while the current task owns the root lease."""

        if (
            not _is_sha256(plan_id)
            or not _is_sha256(source_revision)
            or type(ttl_seconds) is not int
            or ttl_seconds <= 0
            or (
                operation_id is not None
                and (
                    not isinstance(operation_id, str)
                    or _OPERATION_ID_PATTERN.fullmatch(operation_id) is None
                )
            )
        ):
            raise ValueError("generation stage grant 参数无效")
        layout = getattr(config, "layout", None)
        if layout is None:
            raise TypeError("config 必须提供 RuntimeLayout")
        require_active_data_root_writer(layout, owner="embedding_generation")
        now = clock or _now_millis
        expires_at_ms = now() + ttl_seconds * 1000
        if type(expires_at_ms) is not int or expires_at_ms <= now():
            raise ValueError("generation stage grant 时钟无效")

        try:
            layout.ensure_user_directories()
            vectors_root = Path(layout.vector_index_dir)
            staging_root = vectors_root / "staging"
            layout.validate_user_directory(vectors_root, label="向量索引目录", allow_missing=False)
            layout.validate_user_directory(staging_root, label="Embedding staging 目录", allow_missing=True)
            if not staging_root.exists():
                staging_root.mkdir()
            layout.validate_user_directory(staging_root, label="Embedding staging 目录", allow_missing=False)

            resolved_operation_id = operation_id or f"embedding-rebuild-{uuid.uuid4().hex}"
            owner_fence = uuid.uuid4().hex
            stage_dir = staging_root / f".stage-g-{plan_id[:16]}-{owner_fence[:16]}"
            layout.validate_user_directory(
                stage_dir,
                label="Embedding private staging generation",
                allow_missing=True,
            )
            stage_dir.mkdir()
            layout.validate_user_directory(
                stage_dir,
                label="Embedding private staging generation",
                allow_missing=False,
            )
            marker = {
                "schema_version": _SCHEMA_VERSION,
                "operation_id": resolved_operation_id,
                "owner_fence": owner_fence,
                "plan_id": plan_id,
                "source_revision": source_revision,
                "expires_at_ms": expires_at_ms,
            }
            raw = (
                json.dumps(marker, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            layout.atomic_publish_user_file(
                stage_dir / _MARKER_NAME,
                label="Embedding private staging grant",
                data=raw,
            )
            grant = cls(
                layout=layout,
                stage_dir=stage_dir,
                operation_id=resolved_operation_id,
                owner_fence=owner_fence,
                plan_id=plan_id,
                source_revision=source_revision,
                expires_at_ms=expires_at_ms,
                _clock=now,
            )
            object.__setattr__(grant, "_capability", _ISSUED_CAPABILITY)
            return grant
        except PKVRuntimeError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise _safe_error(
                ErrorCode.REPAIR_REQUIRED,
                "无法安全分配 Embedding private staging generation。",
                stage="embedding_stage_grant",
            ) from exc

    def require_stage_write(self, layout: Any, path: Path) -> None:
        """Authorize only the exact private stage after durable revalidation."""

        if (
            type(self) is not GenerationStageGrant
            or getattr(self, "_capability", None) is not _ISSUED_CAPABILITY
        ):
            raise _safe_error(
                ErrorCode.WRITE_BUSY,
                "Embedding private staging capability 不可验证。",
                stage="embedding_stage_grant",
            )
        if layout is not self.layout:
            raise _safe_error(
                ErrorCode.WRITE_BUSY,
                "Embedding private staging 不属于当前运行时布局。",
                stage="embedding_stage_grant",
            )
        if self._clock() > self.expires_at_ms:
            raise _safe_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding private staging capability 已过期；请重新生成计划。",
                stage="embedding_stage_grant",
            )
        requested = Path(path)
        if requested != self.stage_dir:
            raise _safe_error(
                ErrorCode.WRITE_BUSY,
                "Embedding private staging capability 不能写入该路径。",
                stage="embedding_stage_grant",
            )
        try:
            self.layout.validate_user_directory(
                self.stage_dir,
                label="Embedding private staging generation",
                allow_missing=False,
            )
            self.layout.validate_user_file(
                self.marker_path,
                label="Embedding private staging grant",
                allow_missing=False,
            )
            with self.layout.open_user_file(
                self.marker_path,
                "rb",
                label="Embedding private staging grant",
            ) as source:
                payload = json.loads(source.read().decode("utf-8"))
            expected = {
                "schema_version": _SCHEMA_VERSION,
                "operation_id": self.operation_id,
                "owner_fence": self.owner_fence,
                "plan_id": self.plan_id,
                "source_revision": self.source_revision,
                "expires_at_ms": self.expires_at_ms,
            }
            if payload != expected:
                raise ValueError("stage grant marker mismatch")
        except PKVRuntimeError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _safe_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding private staging capability 不可验证；请重新生成计划。",
                stage="embedding_stage_grant",
            ) from exc

    def consume_for_publish(self) -> None:
        """Retire the private marker under the root lease before final publish.

        The final generation must contain only validated index artifacts and its
        manifest.  Removing this marker also prevents a stale in-memory grant
        from authorizing any later stage mutation after the owner has entered
        the irreversible publication phase.
        """

        require_active_data_root_writer(self.layout, owner="embedding_generation")
        self.require_stage_write(self.layout, self.stage_dir)
        try:
            os.unlink(self.marker_path)
            if os.path.lexists(self.marker_path):
                raise OSError("stage grant marker still exists")
        except OSError as exc:
            raise _safe_error(
                ErrorCode.REPAIR_REQUIRED,
                "无法安全封存 Embedding private staging capability。",
                stage="embedding_stage_grant",
            ) from exc


__all__ = ["GenerationStageGrant"]
