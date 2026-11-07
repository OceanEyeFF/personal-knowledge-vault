"""Private, read-only integrity proof for one completed manual vector stage.

This is controlled-file integrity evidence, not a signature against the local
user. No lookup scans directories or renews the original stage capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.runtime.errors import ErrorCode, PKVRuntimeError


MARKER_NAME = ".generation-stage-grant.json"
PROOF_NAME = ".publish-ready.json"
MANIFEST_NAME = "generation-manifest.json"
ARTIFACT_NAMES = (
    "doc_vectors.idx", "doc_vectors_metadata.json",
    "chunk_vectors.idx", "chunk_vectors_metadata.json",
)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")
_OPERATION = re.compile(r"embedding-rebuild-[0-9a-f]{32}\Z")
_STAGE_KEY = re.compile(r"\.stage-g-[0-9a-f]{16}-[0-9a-f]{16}\Z")
_GENERATION = re.compile(r"g-[a-z0-9][a-z0-9-]{7,95}\Z")
_BINDINGS = frozenset({
    "operation_id", "plan_id", "public_plan_id", "source_revision",
    "data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256",
})
_MARKER_FIELDS = _BINDINGS | {
    "schema_version", "owner_fence", "stage_key", "issued_at_ms", "expires_at_ms",
}
_PROOF_FIELDS = _BINDINGS | {
    "schema", "policy_fingerprint", "grant_sha256", "owner_fence", "stage_key",
    "sealed_at_ms", "generation_id", "manifest_sha256", "artifact_sha256",
    "provider_effect", "provider_call_count", "provider_usage", "usage_completeness",
    "reservation_id",
}
_EXPECTED_REQUIRED = _BINDINGS | {"policy_fingerprint", "grant_sha256", "reservation_id"}
_EXPECTED_ALLOWED = _EXPECTED_REQUIRED | {
    "owner_fence", "proof_sha256", "generation_id", "manifest_sha256", "stage_key",
}
_USAGE_DIMENSIONS = {
    "uncached_input_tokens", "cached_input_tokens", "generated_tokens", "embedding_input_tokens",
}


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _require_pattern(value: object, pattern: re.Pattern[str]) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError("invalid identity")


def _require_int(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError("invalid integer")


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _no_float(value: str) -> None:
    raise ValueError("non-integer JSON number")


def _decode(raw: bytes) -> dict[str, Any]:
    result = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_pairs,
        parse_float=_no_float, parse_constant=_no_float,
    )
    if not isinstance(result, dict):
        raise ValueError("JSON record must be an object")
    return result


def canonical_record(payload: Mapping[str, Any]) -> bytes:
    """The frozen canonical JSON encoding, including its single terminal LF."""
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    _decode(raw)  # Refuse floats even when they would be finite JSON numbers.
    return raw


def decode_canonical_record(raw: bytes) -> dict[str, Any]:
    payload = _decode(raw)
    if canonical_record(payload) != raw:
        raise ValueError("noncanonical JSON record")
    return payload


def _read(layout: Any, path: Path) -> bytes:
    layout.validate_user_file(path, label="Embedding private stage 文件", allow_missing=False)
    with layout.open_user_file(path, "rb", label="Embedding private stage 文件") as stream:
        return stream.read()


def _file_hash(layout: Any, path: Path) -> str:
    layout.validate_user_file(path, label="Embedding private stage 文件", allow_missing=False)
    digest = hashlib.sha256()
    with layout.open_user_file(path, "rb", label="Embedding private stage 文件") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_marker(marker: Mapping[str, Any], stage_key: str) -> None:
    if set(marker) != _MARKER_FIELDS or type(marker["schema_version"]) is not int or marker["schema_version"] != 2:
        raise ValueError("invalid marker schema")
    _require_pattern(marker["operation_id"], _OPERATION)
    _require_pattern(marker["owner_fence"], _HEX_ID)
    for name in _BINDINGS - {"operation_id"}:
        _require_pattern(marker[name], _SHA)
    _require_pattern(marker["stage_key"], _STAGE_KEY)
    if marker["stage_key"] != stage_key or stage_key != f".stage-g-{marker['plan_id'][:16]}-{marker['owner_fence'][:16]}":
        raise ValueError("stage identity mismatch")
    _require_int(marker["issued_at_ms"])
    _require_int(marker["expires_at_ms"])
    if marker["expires_at_ms"] <= marker["issued_at_ms"]:
        raise ValueError("invalid grant lifetime")


def _validate_usage(usage: object, completeness: object) -> None:
    if completeness not in ("complete", "partial", "unknown"):
        raise ValueError("invalid usage completeness")
    if completeness == "unknown":
        if usage is not None:
            raise ValueError("unknown usage must remain null")
        return
    if not isinstance(usage, dict) or set(usage) != _USAGE_DIMENSIONS | {"source"}:
        raise ValueError("invalid provider usage shape")
    if usage["source"] != "provider_reported":
        raise ValueError("invalid provider usage source")
    known = 0
    for name in _USAGE_DIMENSIONS:
        if usage[name] is not None:
            _require_int(usage[name])
            known += 1
    if not known:
        raise ValueError("empty provider usage")


def _validate_proof(proof: Mapping[str, Any], marker: Mapping[str, Any], grant_sha256: str) -> None:
    if set(proof) != _PROOF_FIELDS or proof["schema"] != "pkv.embedding.publish-ready.v1":
        raise ValueError("invalid proof schema")
    for name in _BINDINGS | {"owner_fence", "stage_key"}:
        if proof[name] != marker[name]:
            raise ValueError("proof binding mismatch")
    for name in ("policy_fingerprint", "grant_sha256", "manifest_sha256"):
        _require_pattern(proof[name], _SHA)
    if proof["grant_sha256"] != grant_sha256:
        raise ValueError("marker digest mismatch")
    _require_int(proof["sealed_at_ms"])
    if not marker["issued_at_ms"] <= proof["sealed_at_ms"] <= marker["expires_at_ms"]:
        raise ValueError("seal outside grant lifetime")
    _require_pattern(proof["generation_id"], _GENERATION)
    _require_pattern(proof["reservation_id"], _HEX_ID)
    if proof["provider_effect"] != "completed":
        raise ValueError("stage provider effect is not complete")
    _require_int(proof["provider_call_count"])
    _validate_usage(proof["provider_usage"], proof["usage_completeness"])
    artifacts = proof["artifact_sha256"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_NAMES):
        raise ValueError("invalid artifact set")
    for value in artifacts.values():
        _require_pattern(value, _SHA)


def _artifact_facts(layout: Any, stage_dir: Path, generation_id: str, manifest_sha256: str) -> dict[str, str]:
    manifest_raw = _read(layout, stage_dir / MANIFEST_NAME)
    if _hash(manifest_raw) != manifest_sha256:
        raise ValueError("manifest digest mismatch")
    manifest = _decode(manifest_raw)
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("generation_id") != generation_id:
        raise ValueError("manifest identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(ARTIFACT_NAMES):
        raise ValueError("manifest artifacts mismatch")
    actual = {name: _file_hash(layout, stage_dir / name) for name in ARTIFACT_NAMES}
    if actual != files:
        raise ValueError("artifact digest mismatch")
    return actual


@dataclass(frozen=True)
class ValidatedStageProof:
    """Internal result; never expose this object through a public DTO."""

    stage_dir: Path
    payload: dict[str, Any]
    proof_sha256: str

    @property
    def generation_id(self) -> str:
        return self.payload["generation_id"]

    @property
    def manifest_sha256(self) -> str:
        return self.payload["manifest_sha256"]


def validate_publish_ready(layout: Any, *, stage_key: str, expected: Mapping[str, Any]) -> ValidatedStageProof:
    """Validate one ledger-selected sealed stage without writing or checking now.

    The seal's own time must fall inside its original grant. Its present age is
    irrelevant to reading already completed work under a new publication lease.
    The caller also reopens vectors strictly before publication.
    """
    try:
        _require_pattern(stage_key, _STAGE_KEY)
        if not isinstance(expected, Mapping) or not _EXPECTED_REQUIRED <= set(expected) or not set(expected) <= _EXPECTED_ALLOWED:
            raise ValueError("incomplete expected stage binding")
        stage_dir = Path(layout.vector_index_dir) / "staging" / stage_key
        layout.validate_user_directory(stage_dir, label="Embedding private staging generation", allow_missing=False)
        marker_raw = _read(layout, stage_dir / MARKER_NAME)
        marker = decode_canonical_record(marker_raw)
        _validate_marker(marker, stage_key)
        proof_raw = _read(layout, stage_dir / PROOF_NAME)
        proof = decode_canonical_record(proof_raw)
        _validate_proof(proof, marker, _hash(marker_raw))
        proof_sha256 = _hash(proof_raw)
        for key, value in expected.items():
            if key == "proof_sha256":
                if value is not None and value != proof_sha256:
                    raise ValueError("proof digest mismatch")
            elif proof[key] != value:
                raise ValueError("expected operation binding mismatch")
        artifacts = _artifact_facts(layout, stage_dir, proof["generation_id"], proof["manifest_sha256"])
        if artifacts != proof["artifact_sha256"]:
            raise ValueError("sealed artifacts mismatch")
        return ValidatedStageProof(stage_dir, proof, proof_sha256)
    except PKVRuntimeError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED, "Embedding 封存证据不完整或不可验证。",
            stage="embedding_stage_proof", recoverable=True,
        ) from exc


def seal_publish_ready(
    grant: Any, *, generation_id: str, manifest_sha256: str,
    policy_fingerprint: str, reservation_id: str, provider_call_count: int,
    provider_usage: Mapping[str, Any] | None, usage_completeness: str,
) -> ValidatedStageProof:
    """Exclusively publish the completed proof inside the original narrow grant."""
    try:
        if grant.schema_version != 2:
            raise ValueError("manual proof requires a bound grant")
        grant.require_stage_write(grant.layout, grant.stage_dir)
        marker = grant._marker_payload()
        _validate_marker(marker, grant.stage_key)
        artifacts = _artifact_facts(grant.layout, grant.stage_dir, generation_id, manifest_sha256)
        proof = {
            **{name: marker[name] for name in _BINDINGS},
            "schema": "pkv.embedding.publish-ready.v1",
            "policy_fingerprint": policy_fingerprint, "grant_sha256": grant.grant_sha256,
            "owner_fence": grant.owner_fence, "stage_key": grant.stage_key,
            "sealed_at_ms": grant._clock(), "generation_id": generation_id,
            "manifest_sha256": manifest_sha256, "artifact_sha256": artifacts,
            "provider_effect": "completed", "provider_call_count": provider_call_count,
            "provider_usage": dict(provider_usage) if provider_usage is not None else None,
            "usage_completeness": usage_completeness, "reservation_id": reservation_id,
        }
        _validate_proof(proof, marker, grant.grant_sha256)
        raw = canonical_record(proof)
        grant.require_stage_write(grant.layout, grant.stage_dir)
        # 'xb' is exclusive, follows the layout's no-follow/descriptor gateway,
        # and never overwrites an earlier seal. An interrupted partial file is
        # unverified and permanently revokes this capability too.
        with grant.layout.open_user_file(grant.stage_dir / PROOF_NAME, "xb", label="Embedding publish-ready proof") as stream:
            object.__setattr__(grant, "_sealed", True)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return validate_publish_ready(
            grant.layout, stage_key=grant.stage_key,
            expected={name: proof[name] for name in _EXPECTED_REQUIRED},
        )
    except PKVRuntimeError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED, "无法安全封存 Embedding 完成证据。",
            stage="embedding_stage_proof", recoverable=True,
        ) from exc


__all__ = ["ValidatedStageProof", "validate_publish_ready"]
