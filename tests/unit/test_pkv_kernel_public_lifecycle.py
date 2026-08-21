"""Black-box K1a/R2 lifecycle contract using only ``pkv_kernel`` imports."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pkv_kernel


def _configured_public_config(tmp_path: Path) -> pkv_kernel.Config:
    """Create a synthetic profile through the supported Config constructor only."""

    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    (profile_root / "config.yaml").write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {"api_key": "public-lifecycle-llm-secret"},
                    "embedding": {
                        "api_key": "public-lifecycle-embedding-secret",
                        "dim": 1536,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return pkv_kernel.Config(
        profile_root=str(profile_root),
        environment={"PKV_DATA_ROOT": str(tmp_path / "data")},
    )


def test_public_lifecycle_inspect_plan_and_unconfirmed_execute_are_safe(tmp_path: Path) -> None:
    """A Wrapper needs no ``src.*`` import to display R2's safe decision boundary."""

    config = _configured_public_config(tmp_path)
    data_root = tmp_path / "data"

    inspection = pkv_kernel.lifecycle.inspect_runtime(config)
    plan = pkv_kernel.lifecycle.plan_runtime(inspection)

    assert inspection.readiness == "setup_required"
    assert plan.inspection.to_dict() == inspection.to_dict()
    assert not data_root.exists()
    serialized = json.dumps(
        {"inspection": inspection.to_dict(), "plan": plan.to_dict()},
        ensure_ascii=False,
    )
    assert "public-lifecycle-llm-secret" not in serialized
    assert "public-lifecycle-embedding-secret" not in serialized
    assert str(tmp_path) not in serialized
    assert "_config" not in repr(plan)
    assert not hasattr(plan, "__dict__")

    with pytest.raises(pkv_kernel.PKVRuntimeError) as captured:
        pkv_kernel.lifecycle.execute_runtime_plan(plan)

    assert captured.value.code is pkv_kernel.ErrorCode.CONFIRMATION_REQUIRED
    assert not data_root.exists()


@pytest.mark.parametrize("invalid_allow_network", ["false", 0, 1, None, object()])
def test_public_lifecycle_requires_a_literal_boolean_network_confirmation(
    tmp_path: Path,
    invalid_allow_network: object,
) -> None:
    config = _configured_public_config(tmp_path)
    data_root = tmp_path / "data"
    plan = pkv_kernel.lifecycle.plan_runtime(pkv_kernel.lifecycle.inspect_runtime(config))

    with pytest.raises(TypeError, match="allow_network"):
        pkv_kernel.lifecycle.confirm_runtime_plan(
            plan,
            allow_network=invalid_allow_network,  # type: ignore[arg-type]
        )

    assert not data_root.exists()


def test_public_lifecycle_confirmed_snapshot_plan_opens_kernel_without_provider_probe(
    tmp_path: Path,
) -> None:
    """A confirmed no-network plan returns the only supported normal Kernel route."""

    config = _configured_public_config(tmp_path)
    try:
        # This is a legacy compatibility fixture only.  It produces a ready DB
        # with a missing runtime snapshot, whose public R2 plan can be executed
        # without a Provider probe because the dimension is explicit.
        legacy_kernel = pkv_kernel.bootstrap_kernel(config)
        inspection = pkv_kernel.lifecycle.inspect_runtime(config)
        plan = pkv_kernel.lifecycle.plan_runtime(inspection)
        action_kinds = [action["kind"] for action in plan.to_dict()["actions"]]
        assert inspection.readiness == "degraded"
        assert action_kinds == ["record_runtime_snapshot"]

        confirmation = pkv_kernel.lifecycle.confirm_runtime_plan(plan)
        execution = pkv_kernel.lifecycle.execute_runtime_plan(plan, confirmation)
        isolated = pkv_kernel.lifecycle.open_kernel_from_execution(
            execution,
            isolated=True,
        )

        assert execution.to_dict()["context_created"] is False
        assert isinstance(isolated, pkv_kernel.KnowledgeKernel)
        # Lifecycle execution re-parses the exact unchanged user-config source
        # while holding its lease, so it deliberately publishes a successor
        # snapshot rather than reusing the pre-plan Config object.
        assert isolated.config is not config
        assert isolated.config.data_root == config.data_root
        assert pkv_kernel.get_kernel() is legacy_kernel
    finally:
        pkv_kernel.reset_kernel()


def test_public_lifecycle_handles_and_kernel_constructor_cannot_be_forged() -> None:
    """Neither raw Core handles nor application graphs are public construction seams."""

    with pytest.raises(TypeError, match="inspect_runtime"):
        pkv_kernel.lifecycle.RuntimeInspection(object())
    with pytest.raises(TypeError, match="factory-only"):
        pkv_kernel.KnowledgeKernel(object())


def test_uninitialized_legacy_kernel_read_cannot_create_a_vault(tmp_path: Path) -> None:
    """Even compatibility accessors must not turn a read into fresh setup state."""

    config = _configured_public_config(tmp_path)
    kernel = pkv_kernel.get_kernel(config)

    with pytest.raises(FileNotFoundError):
        kernel.load_markdown_path(tmp_path / "not-a-vault-entry.md")

    assert not config.data_root.exists()
    assert pkv_kernel.lifecycle.inspect_runtime(config).readiness == "setup_required"
