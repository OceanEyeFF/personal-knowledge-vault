"""Private, immutable AI-derivation patches used by R4 Q2→Q1′.

Provider output never writes Markdown or SQLite directly.  Q2 first normalizes
it into this typed patch spool, then Q1′ commits it against the exact target
revision.  The spool contains no raw Provider response or credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import (
    atomic_publish_file,
    ensure_safe_directory,
    open_user_file_nofollow,
    validate_directory_components,
)
from src.runtime.writer_inventory import require_active_data_root_writer


_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_SPOOL_SCHEMA_VERSION = 1
_STAGE = "r4_derivation_patch"


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


def _require_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return value


def _normalize_tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("tags 必须是字符串列表")
    normalized = tuple(tag.strip() for tag in value if isinstance(tag, str) and tag.strip())
    if len(normalized) != len(value) or len(set(normalized)) != len(normalized):
        raise ValueError("tags 必须是唯一非空字符串")
    if len(normalized) > 12:
        raise ValueError("tags 最多 12 个")
    return normalized


@dataclass(frozen=True)
class DerivationPatch:
    patch_id: str
    derivation_task_id: str
    target_knowledge_id: int
    expected_revision_sha256: str
    input_digest: str
    summary: str
    tags: tuple[str, ...]
    schema_version: int
    output_digest: str

    @classmethod
    def create(
        cls,
        *,
        derivation_task_id: str,
        target_knowledge_id: int,
        expected_revision_sha256: str,
        input_digest: str,
        summary: str,
        tags: list[str] | tuple[str, ...],
        patch_id: str | None = None,
    ) -> "DerivationPatch":
        values = cls._normalized_values(
            patch_id or uuid.uuid4().hex,
            derivation_task_id,
            target_knowledge_id,
            expected_revision_sha256,
            input_digest,
            summary,
            tags,
            _SPOOL_SCHEMA_VERSION,
        )
        return cls(*values, output_digest=_sha256(cls._digest_payload(*values)))

    @staticmethod
    def _normalized_values(
        patch_id: object,
        derivation_task_id: object,
        target_knowledge_id: object,
        expected_revision_sha256: object,
        input_digest: object,
        summary: object,
        tags: object,
        schema_version: object,
    ) -> tuple[str, str, int, str, str, str, tuple[str, ...], int]:
        if schema_version != _SPOOL_SCHEMA_VERSION:
            raise ValueError("DerivationPatch schema_version 不受支持")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 20_000:
            raise ValueError("summary 无效")
        return (
            _require_id(patch_id, label="patch_id"),
            _require_id(derivation_task_id, label="derivation_task_id"),
            _require_positive_int(target_knowledge_id, label="target_knowledge_id"),
            _require_sha256(expected_revision_sha256, label="expected_revision_sha256"),
            _require_sha256(input_digest, label="input_digest"),
            summary.strip(),
            _normalize_tags(tags),
            _SPOOL_SCHEMA_VERSION,
        )

    @staticmethod
    def _digest_payload(
        patch_id: str,
        derivation_task_id: str,
        target_knowledge_id: int,
        expected_revision_sha256: str,
        input_digest: str,
        summary: str,
        tags: tuple[str, ...],
        schema_version: int,
    ) -> dict[str, object]:
        del patch_id
        return {
            "schema_version": schema_version,
            "derivation_task_id": derivation_task_id,
            "target_knowledge_id": target_knowledge_id,
            "expected_revision_sha256": expected_revision_sha256,
            "input_digest": input_digest,
            "summary": summary,
            "tags": list(tags),
        }

    def to_payload(self) -> dict[str, object]:
        values = self._normalized_values(
            self.patch_id,
            self.derivation_task_id,
            self.target_knowledge_id,
            self.expected_revision_sha256,
            self.input_digest,
            self.summary,
            self.tags,
            self.schema_version,
        )
        if self.output_digest != _sha256(self._digest_payload(*values)):
            raise ValueError("DerivationPatch output_digest 不匹配")
        return {
            "schema_version": self.schema_version,
            "patch_id": self.patch_id,
            "derivation_task_id": self.derivation_task_id,
            "target_knowledge_id": self.target_knowledge_id,
            "expected_revision_sha256": self.expected_revision_sha256,
            "input_digest": self.input_digest,
            "summary": self.summary,
            "tags": list(self.tags),
            "output_digest": self.output_digest,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "DerivationPatch":
        if not isinstance(payload, Mapping):
            raise ValueError("DerivationPatch spool 不是对象")
        if set(payload) != {
            "schema_version",
            "patch_id",
            "derivation_task_id",
            "target_knowledge_id",
            "expected_revision_sha256",
            "input_digest",
            "summary",
            "tags",
            "output_digest",
        }:
            raise ValueError("DerivationPatch spool 字段无效")
        values = cls._normalized_values(
            payload.get("patch_id"),
            payload.get("derivation_task_id"),
            payload.get("target_knowledge_id"),
            payload.get("expected_revision_sha256"),
            payload.get("input_digest"),
            payload.get("summary"),
            payload.get("tags"),
            payload.get("schema_version"),
        )
        digest = _require_sha256(payload.get("output_digest"), label="output_digest")
        if digest != _sha256(cls._digest_payload(*values)):
            raise ValueError("DerivationPatch output_digest 不匹配")
        return cls(*values, output_digest=digest)


@dataclass(frozen=True)
class DerivationPatchReference:
    patch_id: str
    payload_sha256: str


class DerivationPatchSpool:
    def __init__(self, layout: Any) -> None:
        self._layout = layout
        self._root = Path(layout.runtime_state_dir) / "r4" / "patches"

    def _root_for_write(self) -> Path:
        require_active_data_root_writer(self._layout, owner="r4_derivation_patch_spool")
        return ensure_safe_directory(self._root, label="R4 DerivationPatch spool")

    def _root_for_read(self) -> Path:
        return validate_directory_components(self._root, label="R4 DerivationPatch spool")

    @staticmethod
    def _path(root: Path, patch_id: str) -> Path:
        return root / f"{_require_id(patch_id, label='patch_id')}.json"

    def write(self, patch: DerivationPatch) -> DerivationPatchReference:
        if not isinstance(patch, DerivationPatch):
            raise TypeError("patch 必须是 DerivationPatch")
        root = self._root_for_write()
        target = self._path(root, patch.patch_id)
        encoded = _canonical_bytes(patch.to_payload()) + b"\n"
        digest = hashlib.sha256(encoded).hexdigest()
        if os.path.lexists(target):
            existing = self.read(DerivationPatchReference(patch.patch_id, digest))
            if existing.to_payload() != patch.to_payload():
                raise PKVRuntimeError(
                    ErrorCode.RUNTIME_PLAN_STALE,
                    "同一 DerivationPatch identity 的 payload 已变化。",
                    stage=_STAGE,
                    recoverable=True,
                )
            return DerivationPatchReference(patch.patch_id, digest)
        atomic_publish_file(target, label="R4 DerivationPatch", data=encoded)
        return DerivationPatchReference(patch.patch_id, digest)

    def read(self, reference: DerivationPatchReference) -> DerivationPatch:
        if not isinstance(reference, DerivationPatchReference):
            raise TypeError("reference 必须是 DerivationPatchReference")
        patch_id = _require_id(reference.patch_id, label="patch_id")
        expected = _require_sha256(reference.payload_sha256, label="payload_sha256")
        target = self._path(self._root_for_read(), patch_id)
        try:
            with open_user_file_nofollow(
                target,
                "rb",
                label="R4 DerivationPatch",
            ) as handle:
                encoded = handle.read()
        except (OSError, PKVRuntimeError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "DerivationPatch 无法安全读取。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        if hashlib.sha256(encoded).hexdigest() != expected:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "DerivationPatch 摘要不匹配。",
                stage=_STAGE,
                recoverable=True,
            )
        try:
            patch = DerivationPatch.from_payload(json.loads(encoded.decode("utf-8")))
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "DerivationPatch 不可验证。",
                stage=_STAGE,
                recoverable=True,
            ) from exc
        if patch.patch_id != patch_id:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "DerivationPatch identity 不匹配。",
                stage=_STAGE,
                recoverable=True,
            )
        return patch


__all__ = [
    "DerivationPatch",
    "DerivationPatchReference",
    "DerivationPatchSpool",
]
