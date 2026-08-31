"""R2 lifecycle contract: inspect, plan, confirm, then mutate."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Self

import pytest

import src.runtime.lifecycle as lifecycle_module
from src.runtime import (
    ErrorCode,
    PKVRuntimeError,
    RuntimeActionKind,
    RuntimeLayout,
    RuntimeReadiness,
    bootstrap_runtime,
    confirm_runtime_plan,
    execute_runtime_plan,
    inspect_runtime,
    plan_runtime,
    write_lease_scope,
)
from src.storage.coordinator import StorageOperationJournal
from src.storage.migration_manager import MigrationManager
from src.utils import config as config_module
from src.utils.config import Config

PROJECT_ROOT = Path(__file__).parent.parent.parent


def _layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "user-data",
        profile_root=tmp_path / "profile",
        environment={},
    )


def _configured(
    tmp_path: Path,
    *,
    embedding_dim: int | str = 1536,
) -> tuple[RuntimeLayout, Config]:
    layout = _layout(tmp_path)
    layout.user_config_path.parent.mkdir(parents=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {"api_key": "test-llm-secret"},
                    "embedding": {
                        "api_key": "test-embedding-secret",
                        "dim": embedding_dim,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    config = Config(layout=layout)
    return layout, config


def _runtime_snapshot_payload(
    config: Config,
    *,
    database_schema_version: str = "1.2.5",
) -> dict[str, object]:
    """Return the smallest strict v1 runtime snapshot for lifecycle fixtures."""

    assert config.embedding_dim is not None
    return {
        "schema_version": 1,
        "database": {"schema_version": database_schema_version},
        "embedding": {
            "provider": config.embd_provider,
            "fingerprint": config.embedding_index_fingerprint(config.embedding_dim),
        },
    }


class _FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe_llm(self, settings: object) -> None:
        self.calls.append("llm")

    def probe_embedding(self, settings: object) -> int:
        self.calls.append("embedding")
        return 1536


class _DimensionMismatchProbe(_FakeProbe):
    def probe_embedding(self, settings: object) -> int:
        self.calls.append("embedding")
        return 7


class _NoDimensionProbe(_FakeProbe):
    def probe_embedding(self, settings: object) -> None:
        self.calls.append("embedding")
        return None


class _RecordingLease:
    def __init__(self, layout: RuntimeLayout) -> None:
        self._layout = layout
        self.events: list[str] = []
        self._scope = None

    def __enter__(self) -> Self:
        self.events.append("enter")
        self._scope = write_lease_scope(self._layout)
        self._scope.__enter__()
        return self

    def __exit__(self, *args: object) -> None:
        try:
            assert self._scope is not None
            self._scope.__exit__(*args)
        finally:
            self.events.append("exit")


def test_inspect_and_plan_fresh_root_are_strictly_readonly(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.readiness is RuntimeReadiness.SETUP_REQUIRED
    assert inspection.database_state == "fresh"
    assert not layout.user_data_root.exists()
    assert [action.kind for action in plan.actions] == [
        RuntimeActionKind.VALIDATE_PROVIDERS,
        RuntimeActionKind.INITIALIZE_FRESH,
        RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT,
    ]
    assert all(action.requires_confirmation for action in plan.actions)
    assert plan.actions[0].requires_network is True
    serialized = json.dumps({"inspection": inspection.to_dict(), "plan": plan.to_dict()})
    assert "test-llm-secret" not in serialized
    assert "test-embedding-secret" not in serialized


def test_inspect_and_plan_never_construct_provider_or_mutate_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, config = _configured(tmp_path)

    class _UnexpectedLiveProbe:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("inspect/plan must not construct a Provider probe")

    def _unexpected_bootstrap(*args: object, **kwargs: object) -> object:
        raise AssertionError("inspect/plan must not bootstrap")

    monkeypatch.setattr(lifecycle_module, "LiveProviderProbe", _UnexpectedLiveProbe)
    monkeypatch.setattr(lifecycle_module, "bootstrap_runtime", _unexpected_bootstrap)
    monkeypatch.setattr(
        config,
        "write_runtime_config_snapshot",
        lambda payload: (_ for _ in ()).throw(
            AssertionError("inspect/plan must not write a runtime snapshot")
        ),
    )

    inspection = lifecycle_module.inspect_runtime(config)
    plan = lifecycle_module.plan_runtime(inspection)

    assert inspection.readiness is RuntimeReadiness.SETUP_REQUIRED
    assert plan.actions
    assert not layout.user_data_root.exists()


def test_explicit_lifecycle_config_never_falls_back_to_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, config = _configured(tmp_path)

    def _unexpected_global_config() -> Config:
        raise AssertionError("inspect_runtime must use its explicit Config")

    monkeypatch.setattr(config_module, "get_config", _unexpected_global_config)

    inspection = inspect_runtime(config)

    assert inspection.readiness is RuntimeReadiness.SETUP_REQUIRED


def test_execute_fresh_plan_requires_scoped_network_confirmation(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=False),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED
    assert probe.calls == []
    assert not layout.user_data_root.exists()


@pytest.mark.parametrize("invalid_allow_network", ["false", 0, 1, None, object()])
def test_confirmation_rejects_non_boolean_network_permission_before_any_probe(
    tmp_path: Path,
    invalid_allow_network: object,
) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    probe = _FakeProbe()

    with pytest.raises(TypeError, match="allow_network"):
        confirm_runtime_plan(plan, allow_network=invalid_allow_network)  # type: ignore[arg-type]

    assert probe.calls == []
    assert not layout.user_data_root.exists()


def test_execute_rejects_embedding_probe_dimension_mismatch(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    probe = _DimensionMismatchProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert probe.calls == ["llm", "embedding"]
    assert not layout.db_path.exists()


def test_execute_rejects_provider_probe_without_observed_dimension(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    probe = _NoDimensionProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert probe.calls == ["llm", "embedding"]
    assert not layout.db_path.exists()


def test_auto_embedding_dimension_is_persisted_only_after_confirmed_probe(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path, embedding_dim="auto")
    plan = plan_runtime(inspect_runtime(config))

    execution = execute_runtime_plan(
        plan,
        confirm_runtime_plan(plan, allow_network=True),
        provider_probe=_FakeProbe(),
    )

    assert execution.inspection.readiness is RuntimeReadiness.READY
    # Execution reloaded a same-root candidate from the editable source before
    # probing; a fresh immutable Config observes the durable auto-dimension.
    assert Config(layout=layout).embedding_dim == 1536
    snapshot = config.read_runtime_config_snapshot()
    assert snapshot is not None
    assert snapshot["embedding"]["fingerprint"]["embedding_dim"] == "1536"
    assert config.runtime_embedding_dim_path.is_file()


def test_execute_fresh_plan_uses_explicit_probe_and_writer_lease(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    probe = _FakeProbe()
    lease = _RecordingLease(layout)

    execution = execute_runtime_plan(
        plan,
        confirm_runtime_plan(plan, allow_network=True),
        provider_probe=probe,
        writer_lease_factory=lambda explicit_config: lease,
    )

    assert execution.context is not None
    assert execution.context.database.state.value == "ready"
    assert probe.calls == ["llm", "embedding"]
    assert lease.events == ["enter", "exit"]
    assert layout.db_path.is_file()
    assert layout.runtime_config_path.is_file()
    assert execution.inspection.readiness is RuntimeReadiness.READY
    assert execution.provider_validation.llm_actual == "verified"
    assert execution.provider_validation.embedding_actual == "verified"


def test_execute_uses_r3_writer_lease_by_default(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))

    execution = execute_runtime_plan(
        plan,
        confirm_runtime_plan(plan, allow_network=True),
        provider_probe=_FakeProbe(),
    )

    assert execution.inspection.readiness is RuntimeReadiness.READY
    assert (layout.runtime_state_dir / "write.lease").is_file()


@pytest.mark.parametrize("precreate_empty_root", [False, True])
def test_execute_fresh_plan_accepts_r3_lease_anchor(
    tmp_path: Path, precreate_empty_root: bool
) -> None:
    layout, config = _configured(tmp_path)
    if precreate_empty_root:
        layout.user_data_root.mkdir()
    plan = plan_runtime(inspect_runtime(config))

    execution = execute_runtime_plan(
        plan,
        confirm_runtime_plan(plan, allow_network=True),
        provider_probe=_FakeProbe(),
        writer_lease_factory=lambda explicit_config: write_lease_scope(
            explicit_config.layout
        ),
    )

    assert execution.inspection.readiness is RuntimeReadiness.READY
    assert (layout.runtime_state_dir / "write.lease").is_file()


def test_execute_rejects_forged_plan_actions_before_side_effects(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    forged = replace(
        plan,
        actions=(
            replace(
                plan.actions[0],
                requires_confirmation=False,
                requires_network=False,
            ),
            *plan.actions[1:],
        ),
    )
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(forged, None, provider_probe=probe)

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.user_data_root.exists()


def test_execute_rejects_same_root_config_b_swap(tmp_path: Path) -> None:
    layout, config_a = _configured(tmp_path)
    config_b = Config(layout=layout)
    config_b._config["ai"]["llm"]["api_key"] = "test-llm-secret"
    config_b._config["ai"]["embedding"]["api_key"] = "test-embedding-secret"
    plan = plan_runtime(inspect_runtime(config_a))
    swapped = replace(plan, _config=config_b)

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(swapped, confirm_runtime_plan(plan, allow_network=True))

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert not layout.user_data_root.exists()


def test_execute_rejects_stale_plan_before_provider_probe(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    layout.user_data_root.mkdir()
    # A harmless empty root and the R3 lease anchor are both fresh execution
    # infrastructure.  A real data-root artifact is not: it must stale the
    # setup plan before any Provider call or database write.
    (layout.user_data_root / "legacy-marker.txt").write_text(
        "legacy\n", encoding="utf-8"
    )
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.db_path.exists()


def test_provider_key_rotation_stales_plan_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    before = json.dumps(plan.to_dict())
    config._config["ai"]["embedding"]["api_key"] = "rotated-embedding-secret"
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.db_path.exists()
    assert "test-embedding-secret" not in before
    assert "rotated-embedding-secret" not in before


def test_external_user_config_key_edit_stales_lifecycle_plan(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    before = json.dumps(plan.to_dict())
    editable_source = json.loads(layout.user_config_path.read_text(encoding="utf-8"))
    editable_source["ai"]["embedding"]["api_key"] = "next-embedding-secret"
    layout.user_config_path.write_text(
        json.dumps(editable_source),
        encoding="utf-8",
    )
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.db_path.exists()
    assert "next-embedding-secret" not in before


def test_external_user_config_root_edit_stales_lifecycle_plan_before_lease(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    editable_source = json.loads(layout.user_config_path.read_text(encoding="utf-8"))
    editable_source["storage"] = {"data_root": str(tmp_path / "other-data-root")}
    layout.user_config_path.write_text(json.dumps(editable_source), encoding="utf-8")
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    # Lifecycle never follows a changed root under an already confirmed plan.
    # A direct Config/Kernel reload has its own data_root_switch_required gate.
    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.user_data_root.exists()
    assert not (tmp_path / "other-data-root").exists()


def test_unreadable_user_config_source_fails_closed_without_runtime_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, config = _configured(tmp_path)

    def _unsafe_source_revision() -> str:
        raise PKVRuntimeError(
            ErrorCode.PATH_LINK_UNSAFE,
            "test-only unsafe source",
            stage="user_config",
            recoverable=True,
        )

    monkeypatch.setattr(config, "user_config_source_revision", _unsafe_source_revision)

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert ErrorCode.REPAIR_REQUIRED.value in {issue.code for issue in inspection.issues}
    assert [action.kind for action in plan.actions] == [RuntimeActionKind.REPAIR_RUNTIME]
    assert plan.actions[0].executable is False
    assert not layout.user_data_root.exists()


def test_snapshot_appearing_after_plan_stales_before_any_write(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    plan = plan_runtime(inspect_runtime(config))
    assert plan.inspection.runtime_snapshot == "missing"
    config.write_runtime_config_snapshot(_runtime_snapshot_payload(config))
    before = layout.runtime_config_path.read_bytes()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(plan, confirm_runtime_plan(plan))

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert layout.runtime_config_path.read_bytes() == before


def test_execute_rejects_database_created_after_fresh_plan(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    plan = plan_runtime(inspect_runtime(config))
    layout.ensure_user_directories()
    MigrationManager(layout.db_path, layout.migrations_dir).initialize_fresh()
    probe = _FakeProbe()

    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(
            plan,
            confirm_runtime_plan(plan, allow_network=True),
            provider_probe=probe,
        )

    assert exc_info.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert layout.db_path.is_file()


def test_existing_nonempty_root_without_database_requires_repair(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    layout.user_data_root.mkdir()
    (layout.user_data_root / "legacy-marker.txt").write_text("legacy\n", encoding="utf-8")
    before = (layout.user_data_root / "legacy-marker.txt").read_bytes()

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert ErrorCode.REPAIR_REQUIRED.value in {issue.code for issue in inspection.issues}
    assert RuntimeActionKind.REPAIR_RUNTIME in {action.kind for action in plan.actions}
    assert (layout.user_data_root / "legacy-marker.txt").read_bytes() == before


def test_old_schema_produces_nonexecuting_upgrade_plan(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    layout.ensure_user_directories()
    manager = MigrationManager(layout.db_path, layout.migrations_dir)
    manager.apply_migration(
        layout.migrations_dir / "001_initial_schema.sql", auto_backup=False
    )
    before = layout.db_path.read_bytes()

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.readiness is RuntimeReadiness.UPGRADE_REQUIRED
    assert [action.kind for action in plan.actions] == [RuntimeActionKind.UPGRADE_DATABASE]
    with pytest.raises(PKVRuntimeError) as exc_info:
        execute_runtime_plan(plan, confirm_runtime_plan(plan))
    assert exc_info.value.code is ErrorCode.REPAIR_REQUIRED
    assert layout.db_path.read_bytes() == before


def test_journal_inspection_never_recovers_or_rewrites_records(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    bootstrap_runtime(config)
    journal = StorageOperationJournal(layout.runtime_state_dir / "operations")
    operation_id = "7" * 32
    path = journal.write(
        operation_id,
        {
            "action": "archive",
            "status": "in_progress",
            "stage": "preparing",
            "journal_schema_version": 1,
            "checkpoint": "journal_created",
            "errors": [],
            "repair_actions": [],
        },
    )
    before = path.read_bytes()

    inspection = inspect_runtime(config)

    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert inspection.journal_record_count == 1
    assert path.read_bytes() == before


def test_runtime_snapshot_reader_is_called_once(tmp_path: Path) -> None:
    _, config = _configured(tmp_path)
    calls: list[str] = []

    def _reader() -> dict[str, object]:
        calls.append("read")
        return _runtime_snapshot_payload(config)

    def _validator() -> None:
        raise AssertionError("reader already owns validation")

    config.read_runtime_config_snapshot = _reader
    config.validate_runtime_config_snapshot = _validator

    inspection = inspect_runtime(config)

    assert inspection.runtime_snapshot == "valid"
    assert calls == ["read"]


def test_lifecycle_revalidates_reader_payload_against_strict_v1_schema(
    tmp_path: Path,
) -> None:
    _, config = _configured(tmp_path)
    malformed = _runtime_snapshot_payload(config)
    malformed["database"] = {"schema_version": "01.2.4"}
    config.read_runtime_config_snapshot = lambda: malformed

    inspection = inspect_runtime(config)

    assert inspection.runtime_snapshot == "invalid"
    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert "runtime_snapshot_invalid" in {issue.code for issue in inspection.issues}


def test_config_runtime_snapshot_writer_is_secret_free_and_not_business_config(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    payload = _runtime_snapshot_payload(config)

    config.write_runtime_config_snapshot(payload)

    assert config.read_runtime_config_snapshot() == payload
    assert layout.runtime_config_path.is_file()
    assert "test-llm-secret" not in layout.runtime_config_path.read_text(encoding="utf-8")
    assert config.get("embedding.provider") is None


def test_lifecycle_snapshot_refresh_preserves_r4_extension(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    context = bootstrap_runtime(config, recover_interrupted=False)
    existing = _runtime_snapshot_payload(config)
    existing["embedding_index"] = {
        "active_generation": "generation-before-r2-refresh",
        "manifest_sha256": "a" * 64,
    }
    config.write_runtime_config_snapshot(existing)

    with write_lease_scope(layout):
        lifecycle_module._write_runtime_snapshot(config, context.database)

    snapshot = config.read_runtime_config_snapshot()
    assert snapshot is not None
    assert snapshot["embedding_index"] == existing["embedding_index"]


def test_config_runtime_snapshot_writer_rejects_secret_before_creating_root(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)

    with pytest.raises(ValueError, match="敏感字段"):
        config.write_runtime_config_snapshot({"api_key": "never-write"})

    assert not layout.user_data_root.exists()


def test_missing_runtime_snapshot_is_visible_and_readonly(tmp_path: Path) -> None:
    layout, config = _configured(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    assert not config.runtime_config_path.exists()

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.runtime_snapshot == "missing"
    assert inspection.readiness is RuntimeReadiness.DEGRADED
    assert [action.kind for action in plan.actions] == [
        RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT
    ]
    assert plan.actions[0].executable is True
    assert not layout.runtime_config_path.exists()

    execution = execute_runtime_plan(plan, confirm_runtime_plan(plan))

    assert execution.inspection.readiness is RuntimeReadiness.READY
    assert layout.runtime_config_path.is_file()


def test_r1_runtime_snapshot_helper_can_mark_a_ready_runtime(tmp_path: Path) -> None:
    _, config = _configured(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    config.runtime_config_path.write_text(
        json.dumps(_runtime_snapshot_payload(config), ensure_ascii=False),
        encoding="utf-8",
    )

    inspection = inspect_runtime(config)

    assert inspection.runtime_snapshot == "valid"
    assert inspection.readiness is RuntimeReadiness.READY
    assert plan_runtime(inspection).actions == ()


def test_ready_runtime_missing_tokenizer_cache_is_visible_before_bm25_reader(
    tmp_path: Path,
) -> None:
    """Inspect must not call a READY root that an explicit reader will reject."""

    layout, config = _configured(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    config.write_runtime_config_snapshot(_runtime_snapshot_payload(config))
    cache_path = layout.tmp_dir / "jieba.cache"
    cache_path.unlink()

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert ErrorCode.REPAIR_REQUIRED.value in {issue.code for issue in inspection.issues}
    assert [action.kind for action in plan.actions] == [RuntimeActionKind.REPAIR_RUNTIME]
    assert not cache_path.exists()


def test_runtime_snapshot_secret_is_rejected_without_serializing_it(tmp_path: Path) -> None:
    _, config = _configured(tmp_path)
    config.read_runtime_config_snapshot = lambda: {"nested": {"api_key": "leak"}}

    inspection = inspect_runtime(config)
    serialized = json.dumps(inspection.to_dict())

    assert inspection.runtime_snapshot == "invalid"
    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert "leak" not in serialized


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1},
        {
            "schema_version": 1,
            "database": {"schema_version": "1.2.5"},
            "embedding": {
                "provider": "openai_compatible",
                "fingerprint": {
                    "base_url_sha256": "0" * 64,
                    "embedding_model": "test-model",
                    "embedding_dim": 1536,
                },
            },
        },
        {
            "schema_version": 1,
            "database": {"schema_version": "1.2.5"},
            "embedding": {
                "provider": "openai_compatible",
                "fingerprint": {
                    "base_url_sha256": "0" * 64,
                    "embedding_model": "test-model",
                    "embedding_dim": "1536",
                },
            },
            "unexpected": "not-a-runtime-extension-mapping",
        },
    ],
)
def test_malformed_secret_free_runtime_snapshots_fail_closed_without_rewrite(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    layout, config = _configured(tmp_path)
    layout.runtime_config_path.parent.mkdir(parents=True)
    layout.runtime_config_path.write_text(json.dumps(payload), encoding="utf-8")
    before = layout.runtime_config_path.read_bytes()

    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.runtime_snapshot == "invalid"
    assert inspection.readiness is RuntimeReadiness.REPAIR_REQUIRED
    assert "runtime_snapshot_invalid" in {issue.code for issue in inspection.issues}
    assert RuntimeActionKind.REPAIR_RUNTIME in {action.kind for action in plan.actions}
    assert layout.runtime_config_path.read_bytes() == before


def test_runtime_snapshot_embedding_contract_drift_is_visible_but_not_rewritten(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    initial_plan = plan_runtime(inspect_runtime(config))
    execution = execute_runtime_plan(
        initial_plan,
        confirm_runtime_plan(initial_plan),
    )
    assert execution.inspection.readiness is RuntimeReadiness.READY
    before = layout.runtime_config_path.read_bytes()

    config._config["ai"]["embedding"]["model"] = "different-embedding-model"
    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)

    assert inspection.runtime_snapshot == "drifted"
    assert inspection.readiness is RuntimeReadiness.DEGRADED
    assert "runtime_snapshot_drift" in {issue.code for issue in inspection.issues}
    assert [action.kind for action in plan.actions] == [RuntimeActionKind.REPAIR_RUNTIME]
    assert plan.actions[0].executable is False
    assert layout.runtime_config_path.read_bytes() == before


def test_runtime_snapshot_database_contract_drift_is_visible_but_not_rewritten(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)
    bootstrap_runtime(config, recover_interrupted=False)
    layout.runtime_config_path.write_text(
        json.dumps(_runtime_snapshot_payload(config, database_schema_version="1.2.3")),
        encoding="utf-8",
    )
    before = layout.runtime_config_path.read_bytes()

    inspection = inspect_runtime(config)

    assert inspection.runtime_snapshot == "drifted"
    assert inspection.readiness is RuntimeReadiness.DEGRADED
    assert "runtime_snapshot_drift" in {issue.code for issue in inspection.issues}
    assert layout.runtime_config_path.read_bytes() == before


def test_legacy_bootstrap_keeps_mutating_default_and_can_explicitly_skip_recovery(
    tmp_path: Path,
) -> None:
    layout, config = _configured(tmp_path)

    context = bootstrap_runtime(config, recover_interrupted=False)

    assert context.database.state.value == "ready"
    assert layout.db_path.exists()
    assert not (layout.runtime_state_dir / "operations").exists()
