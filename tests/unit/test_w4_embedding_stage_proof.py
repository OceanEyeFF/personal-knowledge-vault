"""MIG-07/08: synthetic sealed-stage integrity, expiry and writer revocation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.runtime.embedding_stage_proof import (
    ARTIFACT_NAMES, MANIFEST_NAME, MARKER_NAME, PROOF_NAME,
    canonical_record, validate_publish_ready,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.generation_stage import GenerationStageGrant
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path) -> Config:
    return Config(layout=RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT, user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile", environment={},
    ))


def _stage(tmp_path: Path):
    config = _config(tmp_path)
    clock = [1_000]
    with write_lease_scope(config.layout):
        grant = GenerationStageGrant.allocate(
            config, plan_id="1" * 64, source_revision="2" * 64,
            public_plan_id="3" * 64, data_root_identity_sha256="4" * 64,
            runtime_snapshot_sha256="5" * 64, contract_sha256="6" * 64,
            operation_id="embedding-rebuild-" + "7" * 32,
            ttl_seconds=1, clock=lambda: clock[0],
        )
    # Synthetic byte-level fixture; lifecycle tests separately exercise strict
    # HNSW/metadata opening before seal and before publication.
    files = {}
    for name in ARTIFACT_NAMES:
        grant.require_stage_write(config.layout, grant.stage_dir)
        raw = ("synthetic-" + name).encode("utf-8")
        config.layout.atomic_publish_user_file(grant.stage_dir / name, data=raw)
        files[name] = hashlib.sha256(raw).hexdigest()
    generation_id = "g-" + "8" * 20 + "-" + "9" * 12
    manifest = {
        "schema_version": 1, "generation_id": generation_id,
        "contract": {}, "source": {}, "files": files,
    }
    manifest_raw = canonical_record(manifest)
    config.layout.atomic_publish_user_file(grant.stage_dir / MANIFEST_NAME, data=manifest_raw)
    facts = {
        "generation_id": generation_id,
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "policy_fingerprint": "a" * 64, "reservation_id": "b" * 32,
        "provider_call_count": 2, "provider_usage": None, "usage_completeness": "unknown",
    }
    expected = {
        key: getattr(grant, key)
        for key in (
            "operation_id", "plan_id", "public_plan_id", "source_revision",
            "data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256", "grant_sha256",
        )
    }
    expected.update(policy_fingerprint=facts["policy_fingerprint"], reservation_id=facts["reservation_id"])
    return config, grant, clock, facts, expected


def test_v2_marker_and_seal_have_exact_canonical_raw_digests(tmp_path: Path) -> None:
    config, grant, clock, facts, expected = _stage(tmp_path)
    marker_raw = grant.marker_path.read_bytes()
    marker = json.loads(marker_raw)
    assert marker_raw == canonical_record(marker)
    assert marker["schema_version"] == 2
    assert marker["stage_key"] == grant.stage_key
    assert marker["issued_at_ms"] == 1_000
    assert marker["expires_at_ms"] == 2_000
    assert grant.grant_sha256 == hashlib.sha256(marker_raw).hexdigest()

    proof = grant.seal_publish_ready(**facts)
    raw = (grant.stage_dir / PROOF_NAME).read_bytes()
    assert proof.proof_sha256 == hashlib.sha256(raw).hexdigest()
    assert raw == canonical_record(proof.payload)
    assert proof.payload["provider_usage"] is None
    assert proof.payload["provider_effect"] == "completed"
    assert set(proof.payload["artifact_sha256"]) == set(ARTIFACT_NAMES)
    assert proof.generation_id == facts["generation_id"]
    assert proof.manifest_sha256 == facts["manifest_sha256"]
    assert validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected) == proof


def test_complete_files_without_seal_do_not_prove_awaiting_publish(tmp_path: Path) -> None:
    config, grant, _, _, expected = _stage(tmp_path)
    with pytest.raises(PKVRuntimeError):
        validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected)
    assert not (grant.stage_dir / PROOF_NAME).exists()
    grant.require_stage_write(config.layout, grant.stage_dir)


def test_seal_revokes_every_later_write_and_cannot_be_overwritten(tmp_path: Path) -> None:
    config, grant, _, facts, _ = _stage(tmp_path)
    grant.seal_publish_ready(**facts)
    raw = (grant.stage_dir / PROOF_NAME).read_bytes()
    with pytest.raises(PKVRuntimeError) as denied:
        grant.require_stage_write(config.layout, grant.stage_dir)
    assert denied.value.code is ErrorCode.RUNTIME_PLAN_STALE
    with pytest.raises(PKVRuntimeError):
        grant.seal_publish_ready(**facts)
    assert (grant.stage_dir / PROOF_NAME).read_bytes() == raw
    # Even an external deletion cannot restore a still-live issued capability.
    (grant.stage_dir / PROOF_NAME).unlink()
    with pytest.raises(PKVRuntimeError):
        grant.require_stage_write(config.layout, grant.stage_dir)


def test_legitimate_seal_is_readable_after_expiry_without_any_write(tmp_path: Path, monkeypatch) -> None:
    config, grant, clock, facts, expected = _stage(tmp_path)
    clock[0] = 2_000
    proof = grant.seal_publish_ready(**facts)
    clock[0] = 2_001
    before = {path.name: path.read_bytes() for path in grant.stage_dir.iterdir()}

    def forbid_write(*args, **kwargs):
        raise AssertionError("read-only inspection attempted a write")

    monkeypatch.setattr(type(config.layout), "atomic_publish_user_file", forbid_write)
    assert validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected) == proof
    with pytest.raises(PKVRuntimeError):
        grant.require_stage_write(config.layout, grant.stage_dir)
    assert {path.name: path.read_bytes() for path in grant.stage_dir.iterdir()} == before


@pytest.mark.parametrize("seal_at", [999, 2_001])
def test_seal_outside_original_grant_lifetime_is_refused(tmp_path: Path, seal_at: int) -> None:
    _, grant, clock, facts, _ = _stage(tmp_path)
    clock[0] = seal_at
    with pytest.raises(PKVRuntimeError):
        grant.seal_publish_ready(**facts)
    assert not (grant.stage_dir / PROOF_NAME).exists()


@pytest.mark.parametrize("name", [MARKER_NAME, PROOF_NAME, MANIFEST_NAME, *ARTIFACT_NAMES])
def test_any_tampered_bound_file_fails_closed(tmp_path: Path, name: str) -> None:
    config, grant, _, facts, expected = _stage(tmp_path)
    grant.seal_publish_ready(**facts)
    target = grant.stage_dir / name
    target.write_bytes(target.read_bytes() + b" ")
    with pytest.raises(PKVRuntimeError):
        validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected)


@pytest.mark.parametrize("name", [MARKER_NAME, PROOF_NAME, MANIFEST_NAME, *ARTIFACT_NAMES])
def test_every_bound_file_is_opened_through_nofollow_gateway(tmp_path: Path, name: str) -> None:
    config, grant, _, facts, expected = _stage(tmp_path)
    grant.seal_publish_ready(**facts)
    os.link(grant.stage_dir / name, tmp_path / "hardlink")
    with pytest.raises(PKVRuntimeError) as denied:
        validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected)
    assert denied.value.code is ErrorCode.DATA_ROOT_UNSAFE


@pytest.mark.parametrize("binding", [
    "operation_id", "plan_id", "public_plan_id", "source_revision",
    "data_root_identity_sha256", "runtime_snapshot_sha256", "contract_sha256",
    "policy_fingerprint", "grant_sha256", "reservation_id", "owner_fence", "proof_sha256",
])
def test_all_operation_bindings_are_revalidated(tmp_path: Path, binding: str) -> None:
    config, grant, _, facts, expected = _stage(tmp_path)
    grant.seal_publish_ready(**facts)
    expected[binding] = "f" * 64
    with pytest.raises(PKVRuntimeError) as invalid:
        validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected)
    assert invalid.value.code is ErrorCode.REPAIR_REQUIRED


@pytest.mark.parametrize("stage_key", ["../outside", "/absolute", ".stage-g-" + "a" * 16, ""])
def test_lookup_cannot_choose_a_stage_path(tmp_path: Path, stage_key: str) -> None:
    config, _, _, _, expected = _stage(tmp_path)
    with pytest.raises(PKVRuntimeError):
        validate_publish_ready(config.layout, stage_key=stage_key, expected=expected)


@pytest.mark.parametrize("mutation", ["duplicate", "extra", "bool_count", "float_count", "unknown_usage", "bad_source", "seal_before", "seal_after", "fence"])
def test_malformed_proof_is_not_reinterpreted_as_completion(tmp_path: Path, mutation: str) -> None:
    config, grant, _, facts, expected = _stage(tmp_path)
    proof = grant.seal_publish_ready(**facts)
    payload = dict(proof.payload)
    if mutation == "duplicate":
        raw = (grant.stage_dir / PROOF_NAME).read_bytes().replace(b'{', b'{"schema":"ignored",', 1)
    else:
        if mutation == "extra":
            payload["extra"] = None
        elif mutation == "bool_count":
            payload["provider_call_count"] = True
        elif mutation == "float_count":
            payload["provider_call_count"] = 2.0
        elif mutation == "unknown_usage":
            payload["provider_usage"] = {"embedding_input_tokens": 1}
        elif mutation == "bad_source":
            payload["usage_completeness"] = "partial"
            payload["provider_usage"] = dict(uncached_input_tokens=None, cached_input_tokens=None,
                generated_tokens=None, embedding_input_tokens=1, source="local_estimate")
        elif mutation == "seal_before":
            payload["sealed_at_ms"] = 999
        elif mutation == "seal_after":
            payload["sealed_at_ms"] = 2_001
        elif mutation == "fence":
            payload["owner_fence"] = "f" * 32
        raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (grant.stage_dir / PROOF_NAME).write_bytes(raw)
    with pytest.raises(PKVRuntimeError):
        validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected)


def test_partial_usage_retains_null_dimensions(tmp_path: Path) -> None:
    _, grant, _, facts, _ = _stage(tmp_path)
    facts.update(usage_completeness="partial", provider_usage={
        "uncached_input_tokens": None, "cached_input_tokens": None,
        "generated_tokens": None, "embedding_input_tokens": 7, "source": "provider_reported",
    })
    proof = grant.seal_publish_ready(**facts)
    assert proof.payload["provider_usage"] == facts["provider_usage"]


def test_interrupted_exclusive_seal_revokes_capability_without_adoptable_proof(tmp_path: Path, monkeypatch) -> None:
    config, grant, _, facts, expected = _stage(tmp_path)
    real_open = type(config.layout).open_user_file

    class InterruptedWrite:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, raw):
            self.stream.write(raw[:8])
            raise OSError("synthetic proof interruption")

    def interrupted(layout, path, mode="r", **kwargs):
        stream = real_open(layout, path, mode, **kwargs)
        return InterruptedWrite(stream) if Path(path).name == PROOF_NAME and mode == "xb" else stream

    monkeypatch.setattr(type(config.layout), "open_user_file", interrupted)
    with pytest.raises(PKVRuntimeError):
        grant.seal_publish_ready(**facts)
    with pytest.raises(PKVRuntimeError):
        grant.require_stage_write(config.layout, grant.stage_dir)
    with pytest.raises(PKVRuntimeError):
        validate_publish_ready(config.layout, stage_key=grant.stage_key, expected=expected)


def test_manual_grant_cannot_consume_its_only_durable_evidence(tmp_path: Path) -> None:
    config, grant, _, facts, _ = _stage(tmp_path)
    grant.seal_publish_ready(**facts)
    before = grant.marker_path.read_bytes()
    with write_lease_scope(config.layout):
        with pytest.raises(PKVRuntimeError):
            grant.consume_for_publish()
    assert grant.marker_path.read_bytes() == before
    assert (grant.stage_dir / PROOF_NAME).exists()


def test_manual_grant_requires_all_frozen_identity_bindings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with write_lease_scope(config.layout):
        with pytest.raises(ValueError):
            GenerationStageGrant.allocate(config, plan_id="a" * 64, source_revision="b" * 64, public_plan_id="c" * 64)
    assert not (config.layout.vector_index_dir / "staging").exists()


def test_uncertain_proof_existence_cannot_authorize_stage_write(tmp_path: Path, monkeypatch) -> None:
    config, grant, _, _, _ = _stage(tmp_path)
    original = os.lstat

    def inaccessible(path, *args, **kwargs):
        if Path(path) == grant.stage_dir / PROOF_NAME:
            raise PermissionError("synthetic inaccessible seal")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", inaccessible)
    with pytest.raises(PKVRuntimeError) as denied:
        grant.require_stage_write(config.layout, grant.stage_dir)
    assert denied.value.code is ErrorCode.RUNTIME_PLAN_STALE
