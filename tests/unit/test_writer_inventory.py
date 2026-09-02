"""R3.1 versioned data-root writer inventory and fail-closed sink contracts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.writer_inventory import (
    DATA_ROOT_WRITER_INVENTORY,
    WRITER_INVENTORY_VERSION,
)
from src.storage.vector_store import VectorStore


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout.resolve(
        resources_root=_PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        environment={},
    )


def test_writer_inventory_is_versioned_and_classifies_every_declared_surface() -> None:
    assert WRITER_INVENTORY_VERSION == 3
    names = [entry.name for entry in DATA_ROOT_WRITER_INVENTORY]
    assert len(names) == len(set(names))
    assert {
        "runtime_lifecycle",
        "application_archive",
        "kernel_mutations",
        "embedding_generation",
        "ai_automation_lifecycle",
        "runtime_audit",
        "runtime_file_logging",
        "offline_test_fixtures",
        "historical_maintenance",
    } <= set(names)
    assert {entry.kind for entry in DATA_ROOT_WRITER_INVENTORY} == {
        "product",
        "test_fixture",
        "historical_fenced",
    }


def test_runtime_snapshot_publish_without_lease_has_zero_side_effects(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = RuntimeSnapshotStore(layout)

    with pytest.raises(PKVRuntimeError) as captured:
        store.publish(store.read(), {})

    assert captured.value.code is ErrorCode.WRITE_BUSY
    assert captured.value.stage == "write_lease"
    assert not layout.user_data_root.exists()


def test_config_bound_writable_vector_store_requires_lease_before_directory_creation(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    config = SimpleNamespace(layout=layout)

    with pytest.raises(PKVRuntimeError) as captured:
        VectorStore(layout.vector_index_dir, dim=3, runtime_config=config)

    assert captured.value.code is ErrorCode.WRITE_BUSY
    assert captured.value.stage == "write_lease"
    assert not layout.user_data_root.exists()
