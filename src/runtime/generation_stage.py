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

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.writer_inventory import require_active_data_root_writer

if TYPE_CHECKING:
    from src.runtime.embedding_stage_proof import ValidatedStageProof


_MARKER_NAME = ".generation-stage-grant.json"
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
    schema_version: int = 1
    public_plan_id: str | None = None
    data_root_identity_sha256: str | None = None
    runtime_snapshot_sha256: str | None = None
    contract_sha256: str | None = None
    issued_at_ms: int | None = None
    _clock: Callable[[], int] = field(
        default=_now_millis,
        repr=False,
        compare=False,
    )
    _capability: object = field(init=False, repr=False, compare=False)
    _grant_sha256: str = field(init=False, repr=False, compare=False)
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def marker_path(self) -> Path:
        return self.stage_dir / _MARKER_NAME

    @property
    def stage_key(self) -> str:
        return self.stage_dir.name

    @property
    def grant_sha256(self) -> str:
        return self._grant_sha256

    def _marker_payload(self) -> dict[str, Any]:
        marker = {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "owner_fence": self.owner_fence,
            "plan_id": self.plan_id,
            "source_revision": self.source_revision,
            "expires_at_ms": self.expires_at_ms,
        }
        if self.schema_version == 2:
            marker.update(
                public_plan_id=self.public_plan_id,
                data_root_identity_sha256=self.data_root_identity_sha256,
                runtime_snapshot_sha256=self.runtime_snapshot_sha256,
                contract_sha256=self.contract_sha256,
                stage_key=self.stage_key,
                issued_at_ms=self.issued_at_ms,
            )
        return marker

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
        public_plan_id: str | None = None,
        data_root_identity_sha256: str | None = None,
        runtime_snapshot_sha256: str | None = None,
        contract_sha256: str | None = None,
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
        bindings = (public_plan_id, data_root_identity_sha256, runtime_snapshot_sha256, contract_sha256)
        manual = any(value is not None for value in bindings)
        if manual and not all(_is_sha256(value) for value in bindings):
            raise ValueError("manual generation stage grant 身份不完整")
        layout = getattr(config, "layout", None)
        if layout is None:
            raise TypeError("config 必须提供 RuntimeLayout")
        require_active_data_root_writer(layout, owner="embedding_generation")
        now = clock or _now_millis
        issued_at_ms = now()
        if type(issued_at_ms) is not int or issued_at_ms < 0:
            raise ValueError("generation stage grant 时钟无效")
        expires_at_ms = issued_at_ms + ttl_seconds * 1000

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
            grant = cls(
                layout=layout,
                stage_dir=stage_dir,
                operation_id=resolved_operation_id,
                owner_fence=owner_fence,
                plan_id=plan_id,
                source_revision=source_revision,
                expires_at_ms=expires_at_ms,
                schema_version=2 if manual else 1,
                public_plan_id=public_plan_id,
                data_root_identity_sha256=data_root_identity_sha256,
                runtime_snapshot_sha256=runtime_snapshot_sha256,
                contract_sha256=contract_sha256,
                issued_at_ms=issued_at_ms if manual else None,
                _clock=now,
            )
            raw = (
                json.dumps(grant._marker_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            layout.atomic_publish_user_file(
                stage_dir / _MARKER_NAME,
                label="Embedding private staging grant",
                data=raw,
            )
            object.__setattr__(grant, "_capability", _ISSUED_CAPABILITY)
            object.__setattr__(grant, "_grant_sha256", hashlib.sha256(raw).hexdigest())
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
        checked_at = self._clock()
        if (type(checked_at) is not int or checked_at > self.expires_at_ms
                or (self.issued_at_ms is not None and checked_at < self.issued_at_ms)):
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
            if self._sealed:
                raise ValueError("stage grant already sealed")
            try:
                os.lstat(self.stage_dir / ".publish-ready.json")
            except FileNotFoundError:
                pass
            else:
                raise ValueError("stage grant already sealed")
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
                raw = source.read()
            if self.schema_version == 2:
                from src.runtime.embedding_stage_proof import decode_canonical_record

                payload = decode_canonical_record(raw)
            else:
                payload = json.loads(raw.decode("utf-8"))
            if payload != self._marker_payload() or hashlib.sha256(raw).hexdigest() != self.grant_sha256:
                raise ValueError("stage grant marker mismatch")
        except PKVRuntimeError:
            raise
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _safe_error(
                ErrorCode.RUNTIME_PLAN_STALE,
                "Embedding private staging capability 不可验证；请重新生成计划。",
                stage="embedding_stage_grant",
            ) from exc

    def seal_publish_ready(
        self, *, generation_id: str, manifest_sha256: str,
        policy_fingerprint: str, reservation_id: str, provider_call_count: int,
        provider_usage: Mapping[str, Any] | None, usage_completeness: str,
    ) -> ValidatedStageProof:
        """Seal a manual stage once; the durable proof revokes its write grant."""
        from src.runtime.embedding_stage_proof import seal_publish_ready

        return seal_publish_ready(
            self, generation_id=generation_id, manifest_sha256=manifest_sha256,
            policy_fingerprint=policy_fingerprint, reservation_id=reservation_id,
            provider_call_count=provider_call_count, provider_usage=provider_usage,
            usage_completeness=usage_completeness,
        )

    def consume_for_publish(self) -> None:
        """Retire the private marker under the root lease before final publish.

        The final generation must contain only validated index artifacts and its
        manifest.  Removing this marker also prevents a stale in-memory grant
        from authorizing any later stage mutation after the owner has entered
        the irreversible publication phase.
        """

        require_active_data_root_writer(self.layout, owner="embedding_generation")
        if self.schema_version != 1:
            raise _safe_error(
                ErrorCode.REPAIR_REQUIRED,
                "已绑定的 manual stage 必须通过保留封存证据的发布流程。",
                stage="embedding_stage_grant",
            )
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
