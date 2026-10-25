"""Fenced private stage capability contracts for foreground rebuilds."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.generation_stage import GenerationStageGrant
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope
from src.storage.vector_store import VectorStore
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _config(tmp_path: Path) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    return Config(layout=layout)


def test_manual_rebuild_operation_identity_is_accepted_under_the_writer_lease(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    operation_id = "embedding-rebuild-" + "c" * 32

    with write_lease_scope(config.layout):
        grant = GenerationStageGrant.allocate(
            config,
            plan_id="a" * 64,
            source_revision="b" * 64,
            operation_id=operation_id,
        )

    assert grant.operation_id == operation_id
    assert grant.marker_path.is_file()


def test_stage_allocation_requires_the_current_root_writer_lease(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with pytest.raises(PKVRuntimeError) as missing_lease:
        GenerationStageGrant.allocate(
            config,
            plan_id="a" * 64,
            source_revision="b" * 64,
            operation_id="embedding-rebuild-" + "c" * 32,
        )

    assert missing_lease.value.code is ErrorCode.WRITE_BUSY
    assert not config.layout.user_data_root.exists()


def test_vector_store_rejects_duck_typed_stage_grant_outside_the_writer_lease(
    tmp_path: Path,
) -> None:
    """Only an issued capability may use the narrow staging lease exception."""

    config = _config(tmp_path)
    with write_lease_scope(config.layout):
        issued = GenerationStageGrant.allocate(
            config,
            plan_id="a" * 64,
            source_revision="b" * 64,
            operation_id="embedding-rebuild-" + "d" * 32,
        )

    class DuckTypedGrant:
        def require_stage_write(self, layout, path) -> None:
            return None

    with pytest.raises(TypeError, match="GenerationStageGrant"):
        VectorStore(
            issued.stage_dir,
            dim=3,
            runtime_config=config,
            stage_grant=DuckTypedGrant(),
        )

    reconstructed = GenerationStageGrant(
        layout=config.layout,
        stage_dir=issued.stage_dir,
        operation_id=issued.operation_id,
        owner_fence=issued.owner_fence,
        plan_id=issued.plan_id,
        source_revision=issued.source_revision,
        expires_at_ms=issued.expires_at_ms,
    )
    with pytest.raises(PKVRuntimeError) as unissued:
        VectorStore(
            issued.stage_dir,
            dim=3,
            runtime_config=config,
            stage_grant=reconstructed,
        )

    assert unissued.value.code is ErrorCode.WRITE_BUSY
