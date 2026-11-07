"""W4 strict stage validation and immutable final-copy boundaries."""

from pathlib import Path

import pytest

from src.runtime import embedding_lifecycle as core
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.generation_stage import GenerationStageGrant
from src.runtime.write_lease import write_lease_scope
from tests.unit.test_embedding_lifecycle import (
    _configured, _prepare_r2, _MutableSource, _records, _FakeEmbedder,
)


def _stage(tmp_path: Path):
    _, config = _configured(tmp_path)
    _prepare_r2(config)
    source = _MutableSource(_records())
    inspection = core.inspect_embedding_index(config, source=source)
    plan = core.plan_embedding_rebuild(inspection)
    with write_lease_scope(config.layout):
        grant = GenerationStageGrant.allocate(
            config, plan_id=plan.plan_id,
            source_revision=inspection._config_source_revision,
        )
    stage, generation, manifest_sha = core._build_generation(
        config, plan=plan, captured=inspection._captured_source,
        embedder=_FakeEmbedder(), stage_grant=grant,
    )
    return config, inspection, grant, stage, generation, manifest_sha


def _publish(fixture):
    config, inspection, _, stage, generation, manifest_sha = fixture
    return core._publish_generation_directory(
        config, stage, expected_generation_id=generation,
        expected_manifest_sha=manifest_sha, expected_contract=inspection.contract,
        expected_source=inspection.source, preserve_stage=True,
    )


def test_final_copy_retains_private_evidence_and_is_byte_idempotent(tmp_path: Path):
    fixture = _stage(tmp_path)
    config, inspection, grant, stage, generation, manifest_sha = fixture
    before = {path.name: path.read_bytes() for path in stage.iterdir()}
    core._validate_staged_generation(
        config, stage, expected_generation_id=generation,
        expected_manifest_sha=manifest_sha, expected_contract=inspection.contract,
        expected_source=inspection.source,
    )
    with write_lease_scope(config.layout):
        assert _publish(fixture) == (generation, manifest_sha)
        assert _publish(fixture) == (generation, manifest_sha)
    final = core._generation_dir(config, generation)
    assert set(path.name for path in final.iterdir()) == {
        *core._PAIR_FILENAMES, "generation-manifest.json",
    }
    assert {path.name: path.read_bytes() for path in stage.iterdir()} == before
    assert grant.marker_path.exists()
    for name in (*core._PAIR_FILENAMES, "generation-manifest.json"):
        assert (final / name).read_bytes() == before[name]


def test_partial_final_is_repair_and_never_filled_in(tmp_path: Path):
    fixture = _stage(tmp_path)
    config, _, _, stage, generation, _ = fixture
    final = core._generation_dir(config, generation)
    final.mkdir(parents=True)
    name = core._PAIR_FILENAMES[0]
    (final / name).write_bytes((stage / name).read_bytes())
    before = {path.name: path.read_bytes() for path in final.iterdir()}
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as rejected:
        _publish(fixture)
    assert rejected.value.code is ErrorCode.REPAIR_REQUIRED
    assert {path.name: path.read_bytes() for path in final.iterdir()} == before


def test_conflicting_existing_final_never_overwritten(tmp_path: Path):
    fixture = _stage(tmp_path)
    config, _, _, _, generation, _ = fixture
    with write_lease_scope(config.layout):
        _publish(fixture)
    final = core._generation_dir(config, generation)
    target = final / core._PAIR_FILENAMES[0]
    target.write_bytes(b"different immutable bytes")
    with write_lease_scope(config.layout), pytest.raises(PKVRuntimeError) as rejected:
        _publish(fixture)
    assert rejected.value.code is ErrorCode.REPAIR_REQUIRED
    assert target.read_bytes() == b"different immutable bytes"


def test_strict_stage_reader_rejects_broken_hnsw_even_with_matching_hashes(tmp_path: Path):
    fixture = _stage(tmp_path)
    config, inspection, _, stage, generation, _ = fixture
    import hashlib
    import json

    artifact = stage / "doc_vectors.idx"
    artifact.write_bytes(b"invalid hnsw binary")
    manifest_path = stage / "generation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][artifact.name] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    raw = json.dumps(manifest, sort_keys=True).encode("utf-8")
    manifest_path.write_bytes(raw)
    with pytest.raises(PKVRuntimeError) as rejected:
        core._validate_staged_generation(
            config, stage, expected_generation_id=generation,
            expected_manifest_sha=hashlib.sha256(raw).hexdigest(),
            expected_contract=inspection.contract, expected_source=inspection.source,
        )
    assert rejected.value.code is ErrorCode.REPAIR_REQUIRED


def test_readonly_old_stage_contract_survives_current_model_change(tmp_path: Path):
    import json
    from src.utils.config import Config

    config, inspection, _, stage, generation, manifest_sha = _stage(tmp_path)
    path = config.layout.user_config_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ai"]["embedding"]["model"] = "new-model-after-seal"
    payload["ai"]["embedding"]["base_url"] = "https://changed.invalid/v1"
    path.write_text(json.dumps(payload), encoding="utf-8")
    changed = Config(layout=config.layout)
    before = {child.name: child.read_bytes() for child in stage.iterdir()}
    manifest, actual_sha = core._validate_staged_generation(
        changed, stage, expected_generation_id=generation,
        expected_manifest_sha=manifest_sha, expected_contract=inspection.contract,
        expected_source=inspection.source,
    )
    assert actual_sha == manifest_sha
    assert manifest["contract"]["model"] != changed.embd_model
    assert {child.name: child.read_bytes() for child in stage.iterdir()} == before


def test_public_v2_a_real_writer_contention_has_no_operation_or_side_effects(tmp_path: Path, monkeypatch):
    from threading import Event, Thread

    from pkv_kernel import embedding
    from src.runtime import embedding_rebuild_v2 as runtime_v2
    from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger
    from src.runtime.runtime_snapshot import RuntimeSnapshotStore
    from tests.unit.test_w4_embedding_publication import _config, _NoDocumentsProvider

    config = _config(tmp_path)
    plan = embedding.plan_embedding_rebuild_v2(embedding.inspect_embedding_rebuild_v2(config))
    confirmation = embedding.confirm_embedding_rebuild_v2(plan, allow_network=True)
    ledger = EmbeddingRebuildLedger(config.layout)
    snapshot_before = RuntimeSnapshotStore(config.layout).read()
    audit_path = config.layout.log_dir / "audit.jsonl"
    audit_before = audit_path.read_bytes() if audit_path.exists() else None
    vectors = config.layout.vector_index_dir

    def vector_tree():
        return {
            path.relative_to(vectors).as_posix(): None if path.is_dir() else path.read_bytes()
            for path in vectors.rglob("*")
        }

    vectors_before = vector_tree()
    assert not ledger.path.exists()
    provider_constructions = []

    def provider_factory(selected_config):
        assert selected_config is config
        provider_constructions.append(selected_config)
        return _NoDocumentsProvider()

    monkeypatch.setattr(runtime_v2, "create_embedder", provider_factory)
    acquired, release = Event(), Event()
    contender_errors = []

    def hold_writer():
        try:
            with write_lease_scope(config.layout):
                acquired.set()
                if not release.wait(timeout=30):
                    raise AssertionError("test did not release the competing root writer")
        except BaseException as error:
            contender_errors.append(error)
            acquired.set()

    contender = Thread(target=hold_writer, daemon=True)
    contender.start()
    try:
        assert acquired.wait(timeout=15), "competing root writer did not acquire its real lease"
        assert not contender_errors
        with pytest.raises(PKVRuntimeError) as rejected:
            embedding.execute_embedding_rebuild_v2(plan, confirmation)
        assert rejected.value.code is ErrorCode.WRITE_BUSY
        assert "details" not in rejected.value.to_dict()
        assert not provider_constructions
        assert not ledger.path.exists()
        assert ledger.read_operations() == ()
        assert vector_tree() == vectors_before
        assert (audit_path.read_bytes() if audit_path.exists() else None) == audit_before
        assert RuntimeSnapshotStore(config.layout).read() == snapshot_before
    finally:
        release.set()
        contender.join(timeout=15)
    assert not contender.is_alive()
    assert not contender_errors

    result = embedding.execute_embedding_rebuild_v2(
        plan, embedding.confirm_embedding_rebuild_v2(plan, allow_network=True),
    ).to_dict()
    assert result["status"] == "ready" and result["publication_committed"]
    assert len(provider_constructions) == 1
    operations = ledger.read_operations()
    assert len(operations) == 1
    assert operations[0].reservation.state == "settled"
