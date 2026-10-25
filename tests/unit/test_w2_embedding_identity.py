"""W2 confirmation identity contracts; no v2 API or ledger migration."""

import hashlib
import json
from pathlib import Path

import pytest

from pkv_kernel import embedding
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger
from src.runtime.embedding_rebuild_policy import inspect_embedding_rebuild_policy
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.utils.config import Config
from tests.unit.test_embedding_rebuild_ledger import _configured
from tests.unit.test_kernel_embedding_rebuild import _seed_r2


def _canonical_sha256(value: object) -> str:
    """Historical v1 encoding, used only to seed a pre-W2 ledger fixture."""

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.parametrize("change", ["mapping_extension", "raw_bytes"])
def test_er02_full_snapshot_change_invalidates_public_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Unchanged visible index facts cannot hide a changed snapshot CAS revision."""

    config = _configured(tmp_path)
    _seed_r2(config)
    inspection_before = embedding.inspect_embedding_rebuild(config)
    plan_before = embedding.plan_embedding_rebuild(inspection_before)
    snapshot_store = RuntimeSnapshotStore(config.layout)
    snapshot_before = snapshot_store.read()
    with write_lease_scope(config.layout):
        if change == "mapping_extension":
            snapshot_store.publish(
                snapshot_before,
                snapshot_before.merged({"identity_probe": {"revision": 1}}),
            )
        else:
            # Same parsed YAML, different raw-byte CAS identity.
            config.layout.atomic_publish_user_file(
                snapshot_store.path,
                label="synthetic snapshot formatting change",
                data=snapshot_store.path.read_bytes() + b"\n",
            )
    snapshot_after = snapshot_store.read()
    assert snapshot_after.raw_sha256 != snapshot_before.raw_sha256
    if change == "raw_bytes":
        assert snapshot_after.payload == snapshot_before.payload

    # A plan from an already captured inspection keeps the original binding;
    # public identity must not re-read newer state to rewrite that confirmation.
    assert embedding.plan_embedding_rebuild(inspection_before).plan_id == plan_before.plan_id
    inspection_after = embedding.inspect_embedding_rebuild(config)
    before_index = inspection_before.to_dict()["index"]
    after_index = inspection_after.to_dict()["index"]
    before_index.pop("revision")
    after_index.pop("revision")
    assert before_index == after_index
    plan_after = embedding.plan_embedding_rebuild(inspection_after)
    assert plan_after.plan_id != plan_before.plan_id

    provider_calls: list[Config] = []

    def forbidden_provider(current_config: Config):
        provider_calls.append(current_config)
        raise AssertionError("stale confirmation must not construct a Provider")

    monkeypatch.setattr(embedding, "create_embedder", forbidden_provider)
    with pytest.raises(PKVRuntimeError) as stale:
        embedding.execute_embedding_rebuild(
            plan_before,
            embedding.confirm_embedding_rebuild(
                plan_before,
                approved=True,
                allow_network=True,
            ),
        )
    assert stale.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert provider_calls == []
    assert snapshot_store.read() == snapshot_after


def test_key_rotation_does_not_enter_public_identity_or_disclose_private_material(
    tmp_path: Path,
) -> None:
    """Only safe runtime facts enter cross-process confirmation identity."""

    config = _configured(tmp_path)
    _seed_r2(config)
    before = embedding.plan_embedding_rebuild(embedding.inspect_embedding_rebuild(config))
    private_revision_before = config.user_config_source_revision()
    private_config = json.loads(config.layout.user_config_path.read_text(encoding="utf-8"))
    private_config["ai"]["embedding"]["api_key"] = "rotated-synthetic-credential"
    config.layout.user_config_path.write_text(json.dumps(private_config), encoding="utf-8")
    reloaded = Config(layout=config.layout)
    after = embedding.plan_embedding_rebuild(embedding.inspect_embedding_rebuild(reloaded))
    private_revision_after = reloaded.user_config_source_revision()

    assert private_revision_after != private_revision_before
    assert after.plan_id == before.plan_id
    public_payload = json.dumps(after.to_dict(), ensure_ascii=False)
    for private_value in (
        "embedding-test-secret",
        "rotated-synthetic-credential",
        private_revision_before,
        private_revision_after,
        str(config.layout.user_data_root),
        str(config.layout.user_config_path),
    ):
        assert private_value not in public_payload


def test_secret_bearing_runtime_snapshot_cannot_be_hashed_into_public_plan(
    tmp_path: Path,
) -> None:
    """The snapshot digest must come from the validated secret-free reader."""

    config = _configured(tmp_path)
    _seed_r2(config)
    snapshot_store = RuntimeSnapshotStore(config.layout)
    unsafe = snapshot_store.read().payload
    unsafe["unexpected"] = {"api_key": "synthetic-snapshot-credential"}
    raw = json.dumps(unsafe).encode()
    # A test-owned corruption fixture, not a product snapshot publication.
    snapshot_store.path.write_bytes(raw)

    inspection = embedding.inspect_embedding_rebuild(config)
    assert inspection.state == "repair_required"
    with pytest.raises(PKVRuntimeError) as rejected:
        embedding.plan_embedding_rebuild(inspection)
    assert rejected.value.code is ErrorCode.REPAIR_REQUIRED
    payload = json.dumps(inspection.to_dict(), ensure_ascii=False)
    assert "synthetic-snapshot-credential" not in payload
    assert hashlib.sha256(raw).hexdigest() not in payload
    assert snapshot_store.path.read_bytes() == raw


def test_strengthened_confirmation_preserves_existing_v1_active_replay_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing plan IDs cannot authorize a duplicate pre-W2 paid operation."""

    config = _configured(tmp_path)
    _seed_r2(config)
    inspection = embedding.inspect_embedding_rebuild(config)
    public = inspection.to_dict()
    legacy_index = public["index"]
    legacy_index.pop("revision")
    historical_material = {
        "inspection": legacy_index,
        "policy": public["policy"],
        "action": "kernel_embedding_rebuild_v1",
    }
    legacy_replay_key = _canonical_sha256(historical_material)
    legacy_public_plan_id = _canonical_sha256({**historical_material, "force": False})
    ledger = EmbeddingRebuildLedger(config.layout)
    with write_lease_scope(config.layout):
        existing = ledger.begin(
            plan_id="a" * 64,
            public_plan_id=legacy_public_plan_id,
            replay_key=legacy_replay_key,
            source_revision="b" * 64,
            policy=inspect_embedding_rebuild_policy(config),
            estimated_tokens=4,
        )

    strengthened = embedding.plan_embedding_rebuild(inspection)
    assert strengthened.plan_id != legacy_public_plan_id
    provider_calls: list[Config] = []

    def forbidden_provider(current_config: Config):
        provider_calls.append(current_config)
        raise AssertionError("an active historical replay key must block Provider work")

    monkeypatch.setattr(embedding, "create_embedder", forbidden_provider)
    with pytest.raises(PKVRuntimeError) as busy:
        embedding.execute_embedding_rebuild(
            strengthened,
            embedding.confirm_embedding_rebuild(
                strengthened,
                approved=True,
                allow_network=True,
            ),
        )
    assert busy.value.code is ErrorCode.WRITE_BUSY
    assert provider_calls == []
    assert ledger.read_operations() == (existing,)
    assert not (config.layout.vector_index_dir / "staging").exists()
