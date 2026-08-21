"""R1 contract tests for the independent user-config/runtime-snapshot planes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.utils.config import Config


def _runtime_snapshot(*, note: str | None = None) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "database": {"schema_version": "1.2.3"},
        "embedding": {
            "provider": "openai_compatible",
            "fingerprint": {
                "base_url_sha256": "a" * 64,
                "embedding_model": "test-embedding",
                "embedding_dim": "1536",
            },
        },
    }
    if note is not None:
        snapshot["ai"] = {"llm": {"model": note}}
    return snapshot


def _write_bundled_config(resources_root: Path) -> Path:
    config_path = resources_root / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "storage": {},
                "ai": {
                    "llm": {
                        "api_key": "",
                        "base_url": "https://llm.example.test/v1",
                        "model": "bundled-model",
                    },
                    "embedding": {
                        "api_key": "",
                        "base_url": "https://embedding.example.test/v1",
                        "model": "bundled-embedding",
                        "dim": 1536,
                    },
                },
                "logging": {"level": "INFO"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


@pytest.fixture
def isolated_default_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    resources_root = tmp_path / "resources"
    _write_bundled_config(resources_root)
    monkeypatch.setattr(
        "src.runtime.layout._default_resource_root",
        lambda: resources_root,
    )
    return resources_root


def test_user_config_selects_one_root_and_runtime_snapshot_never_merges(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    data_root = tmp_path / "configured-data-root"
    user_config_path = profile_root / "config.yaml"
    user_config_path.parent.mkdir(parents=True)
    user_config_path.write_text(
        yaml.safe_dump(
            {
                "storage": {
                    "data_root": str(data_root),
                    # Product child paths are deliberately ignored; all runtime
                    # locations are derived from the selected one root.
                    "db_path": str(tmp_path / "outside.db"),
                },
                "ai": {"llm": {"model": "user-model"}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    runtime_snapshot_path = data_root / "config" / "local.yaml"
    runtime_snapshot_path.parent.mkdir(parents=True)
    runtime_snapshot = _runtime_snapshot(note="runtime-must-not-merge")
    runtime_snapshot_path.write_text(
        yaml.safe_dump(runtime_snapshot, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    config = Config(profile_root=str(profile_root), environment={})

    assert config.user_config_path == user_config_path
    assert config.runtime_config_path == runtime_snapshot_path
    assert config.data_root == data_root
    assert config.data_root_identity == str(data_root).casefold()
    assert config.db_path == data_root / "db" / "knowledge_vault.db"
    assert config.vault_dir == data_root / "vault"
    assert config.vector_index_dir == data_root / "vectors"
    assert config.llm_model == "user-model"
    assert config.read_runtime_config_snapshot() == runtime_snapshot


def test_environment_whitelist_overrides_user_root_and_log_level_only(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    configured_root = tmp_path / "configured-root"
    environment_root = tmp_path / "environment-root"
    (profile_root / "config.yaml").parent.mkdir(parents=True)
    (profile_root / "config.yaml").write_text(
        yaml.safe_dump(
            {"storage": {"data_root": str(configured_root)}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    config = Config(
        profile_root=str(profile_root),
        environment={
            "PKV_DATA_ROOT": str(environment_root),
            "PKV_LOG_LEVEL": "DEBUG",
            "DATA_DIR": str(tmp_path / "ignored-legacy-root"),
            "LOG_LEVEL": "ERROR",
            "DB_PATH": str(tmp_path / "ignored-legacy.db"),
        },
    )

    assert config.data_root == environment_root
    assert config.db_path == environment_root / "db" / "knowledge_vault.db"
    assert config.log_level == "DEBUG"
    assert not environment_root.exists()


def test_offline_legacy_data_dir_overrides_formal_root_for_both_config_seams(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    """Only the marked offline seam may narrow a formal process root."""

    profile_root = tmp_path / "profile"
    formal_root = tmp_path / "formal-root"
    isolated_root = tmp_path / "isolated-root"
    environment = {
        "PKV_TEST_OFFLINE": "1",
        "PKV_DATA_ROOT": str(formal_root),
        "DATA_DIR": str(isolated_root),
    }

    default_config = Config(
        profile_root=str(profile_root),
        environment=environment,
    )
    explicit_config = Config(
        config_path=str(isolated_default_config / "config" / "config.yaml"),
        profile_root=str(profile_root),
        environment=environment,
    )

    for config in (default_config, explicit_config):
        assert config.data_root == isolated_root
        assert config.db_path == isolated_root / "db" / "knowledge_vault.db"
    assert not formal_root.exists()
    assert not isolated_root.exists()


def test_explicit_config_keeps_formal_root_over_legacy_environment(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    """The explicit config seam does not promote legacy DATA_DIR in product mode."""

    formal_root = tmp_path / "formal-root"
    legacy_root = tmp_path / "legacy-root"
    config = Config(
        config_path=str(isolated_default_config / "config" / "config.yaml"),
        environment={
            "PKV_DATA_ROOT": str(formal_root),
            "DATA_DIR": str(legacy_root),
        },
    )

    assert config.data_root == formal_root
    assert config.db_path == formal_root / "db" / "knowledge_vault.db"
    assert not formal_root.exists()
    assert not legacy_root.exists()


def test_user_config_update_rejects_root_switch_before_persisting(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})

    config.update_user_config(
        {
            "ai.llm.model": "saved-model",
        }
    )

    saved = yaml.safe_load(config.user_config_path.read_text(encoding="utf-8"))
    assert saved["ai"]["llm"]["model"] == "saved-model"
    assert "storage" not in saved

    from src.runtime.errors import ErrorCode, PKVRuntimeError

    with pytest.raises(PKVRuntimeError) as captured:
        config.update_user_config(
            {"storage.data_root": str(tmp_path / "later-data")}
        )

    assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
    assert captured.value.stage == "config_update"
    assert captured.value.recoverable is True
    # No root setting was written that could surprise a subsequent startup.
    saved_after = yaml.safe_load(config.user_config_path.read_text(encoding="utf-8"))
    assert saved_after == saved
    assert not config.data_root.exists()
    assert not config.runtime_config_path.exists()


@pytest.mark.parametrize("root_key", ["data_root", "data_dir"])
def test_environment_root_does_not_mask_user_root_update_intent(
    isolated_default_config: Path,
    tmp_path: Path,
    root_key: str,
) -> None:
    """An env override cannot leave a different user root latent on disk."""

    from src.runtime.errors import ErrorCode, PKVRuntimeError

    profile_root = tmp_path / "profile"
    old_user_root = tmp_path / "old-user-root"
    new_user_root = tmp_path / "new-user-root"
    environment_root = tmp_path / "environment-root"
    user_config_path = profile_root / "config.yaml"
    original = {"storage": {root_key: str(old_user_root)}}
    user_config_path.parent.mkdir(parents=True)
    user_config_path.write_text(
        yaml.safe_dump(original, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    config = Config(
        profile_root=str(profile_root),
        environment={"PKV_DATA_ROOT": str(environment_root)},
    )

    assert config.data_root == environment_root
    with pytest.raises(PKVRuntimeError) as captured:
        config.update_user_config({f"storage.{root_key}": str(new_user_root)})

    assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
    assert captured.value.stage == "config_update"
    assert yaml.safe_load(user_config_path.read_text(encoding="utf-8")) == original
    assert config.data_root == environment_root
    assert not environment_root.exists()
    assert not new_user_root.exists()


def test_config_get_returns_a_deep_defensive_copy(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    (profile_root / "config.yaml").write_text(
        yaml.safe_dump(
            {"retrieval": {"vector": {"top_k": 11}, "labels": ["one"]}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = Config(profile_root=str(profile_root), environment={})

    retrieved = config.get("retrieval")
    retrieved["vector"]["top_k"] = 999
    retrieved["labels"].append("mutated")
    default = {"nested": ["original"]}
    fallback = config.get("does.not.exist", default)
    fallback["nested"].append("mutated")

    assert config.get("retrieval.vector.top_k") == 11
    assert config.get("retrieval.labels") == ["one"]
    assert default == {"nested": ["original"]}


def test_user_config_source_revision_is_opaque_and_detects_external_edits(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})

    missing_revision = config.user_config_source_revision()
    assert missing_revision == config.user_config_source_revision()
    assert len(missing_revision) == 64

    secret = "provider-key-must-not-leak"
    config.user_config_path.parent.mkdir(parents=True)
    config.user_config_path.write_text(
        yaml.safe_dump(
            {"ai": {"llm": {"api_key": secret, "model": "after-edit"}}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    edited_revision = config.user_config_source_revision()
    assert edited_revision != missing_revision
    assert len(edited_revision) == 64
    assert secret not in edited_revision
    assert str(config.user_config_path) not in edited_revision

    config.user_config_path.write_text(
        yaml.safe_dump(
            {"ai": {"llm": {"api_key": secret, "model": "second-edit"}}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    assert config.user_config_source_revision() != edited_revision

    # This marker is deliberately raw-source only: lifecycle can invalidate an
    # old plan safely even when a later explicit reload will reject malformed
    # YAML.  It must not parse/merge the file just to compute the revision.
    config.user_config_path.write_text("ai: [unterminated\n", encoding="utf-8")
    assert config.user_config_source_revision() != edited_revision
    assert config.llm_model == "bundled-model"


def test_manual_root_edit_is_rejected_by_reload_before_publication(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    from src.runtime.errors import ErrorCode, PKVRuntimeError

    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})
    config.user_config_path.parent.mkdir(parents=True)
    config.user_config_path.write_text(
        yaml.safe_dump(
            {"storage": {"data_root": str(tmp_path / "other-root")}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PKVRuntimeError) as captured:
        config.reload_snapshot()

    assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
    assert captured.value.stage == "config_reload"
    assert config.data_root == profile_root / "data"


@pytest.mark.parametrize("root_key", ["data_root", "data_dir"])
def test_environment_root_does_not_mask_manual_user_root_intent_on_reload(
    isolated_default_config: Path,
    tmp_path: Path,
    root_key: str,
) -> None:
    """Reload must reject a manually changed user root even while env wins."""

    from src.runtime.errors import ErrorCode, PKVRuntimeError

    profile_root = tmp_path / "profile"
    old_user_root = tmp_path / "old-user-root"
    new_user_root = tmp_path / "new-user-root"
    environment_root = tmp_path / "environment-root"
    user_config_path = profile_root / "config.yaml"
    user_config_path.parent.mkdir(parents=True)
    user_config_path.write_text(
        yaml.safe_dump(
            {"storage": {root_key: str(old_user_root)}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = Config(
        profile_root=str(profile_root),
        environment={"PKV_DATA_ROOT": str(environment_root)},
    )

    user_config_path.write_text(
        yaml.safe_dump(
            {"storage": {root_key: str(new_user_root)}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(PKVRuntimeError) as captured:
        config.reload_snapshot()

    assert captured.value.code is ErrorCode.DATA_ROOT_SWITCH_REQUIRED
    assert captured.value.stage == "config_reload"
    assert config.data_root == environment_root
    assert config.get(f"storage.{root_key}") == str(old_user_root)
    assert not environment_root.exists()
    assert not new_user_root.exists()


def test_runtime_snapshot_requires_complete_v1_schema_before_read_or_write(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})
    malformed = {
        "schema_version": 1,
        "database": {"schema_version": "not-semver"},
        "embedding": {"provider": "", "fingerprint": {}},
    }

    with pytest.raises(ValueError, match="结构无效"):
        config.write_runtime_config_snapshot(malformed)
    assert not config.data_root.exists()

    config.runtime_config_path.parent.mkdir(parents=True)
    config.runtime_config_path.write_text(
        yaml.safe_dump(malformed, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="结构无效"):
        config.read_runtime_config_snapshot()


def test_runtime_snapshot_compatibility_writer_preserves_other_runtime_extensions(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    """R2 base facts must not erase an R4-owned active-generation pointer."""

    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})
    original = _runtime_snapshot()
    original["embedding"]["active_generation"] = {
        "generation_id": "generation-001",
        "manifest_sha256": "b" * 64,
    }
    config.write_runtime_config_snapshot(original)

    r2_update = _runtime_snapshot(note="lifecycle-refreshed")
    config.write_runtime_config_snapshot(r2_update)

    published = config.read_runtime_config_snapshot()
    assert published is not None
    assert published["ai"]["llm"]["model"] == "lifecycle-refreshed"
    assert published["embedding"]["active_generation"] == original["embedding"][
        "active_generation"
    ]


def test_legacy_local_config_alias_writes_user_config_not_runtime_snapshot(
    isolated_default_config: Path,
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})

    with pytest.deprecated_call(match="update_local_config"):
        config.update_local_config({"ai.llm.model": "legacy-saved-model"})
    with pytest.deprecated_call(match="local_config_path"):
        assert config.local_config_path == config.user_config_path

    saved = yaml.safe_load(config.user_config_path.read_text(encoding="utf-8"))
    assert saved["ai"]["llm"]["model"] == "legacy-saved-model"
    assert not config.runtime_config_path.exists()
    assert not config.data_root.exists()


@pytest.mark.parametrize("sensitive_key", ["api_key", "Authorization", "cookie", "token"])
def test_runtime_snapshot_rejects_sensitive_keys_without_echoing_values(
    isolated_default_config: Path,
    tmp_path: Path,
    sensitive_key: str,
) -> None:
    profile_root = tmp_path / "profile"
    config = Config(profile_root=str(profile_root), environment={})
    secret = "must-not-appear-in-error"
    config.runtime_config_path.parent.mkdir(parents=True)
    config.runtime_config_path.write_text(
        yaml.safe_dump(
            {"nested": {sensitive_key: secret}},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as captured:
        config.validate_runtime_config_snapshot()

    message = str(captured.value)
    assert "敏感字段" in message
    assert secret not in message
    assert sensitive_key not in message
