"""Public Kernel façade contract for manual Embedding rebuilds."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from pkv_kernel import embedding
from pkv_kernel.contracts import KERNEL_CAPABILITIES
from src.runtime.embedding_rebuild_ledger import (
    EmbeddingRebuildLedger,
    EmbeddingRebuildLedgerState,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.runtime.layout import RuntimeLayout
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _configured(tmp_path: Path) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "data",
        profile_root=tmp_path / "profile",
        environment={},
    )
    layout.user_config_path.parent.mkdir(parents=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "embedding": {
                        "api_key": "embedding-test-secret",
                        "base_url": "https://embedding.invalid/v1",
                        "model": "r4-fake-3",
                        "dim": 3,
                    },
                    "embedding_rebuild": {
                        "schema_version": 1,
                        "token_budget": {
                            "timezone": "UTC",
                            "daily_total_tokens": 0,
                            "monthly_total_tokens": 100,
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return Config(layout=layout)


def _seed_r2(config: Config) -> None:
    from src.runtime.bootstrap import bootstrap_runtime

    bootstrap_runtime(config)
    store = RuntimeSnapshotStore(config.layout)
    observed = store.read()
    assert config.embedding_dim is not None
    with write_lease_scope(config.layout):
        store.publish(
            observed,
            {
                "schema_version": 1,
                "database": {"schema_version": "1.2.7"},
                "embedding": {
                    "provider": config.embd_provider,
                    "fingerprint": config.embedding_index_fingerprint(config.embedding_dim),
                },
            },
        )


def test_public_kernel_advertises_opaque_embedding_rebuild_capability() -> None:
    assert "kernel.embedding-rebuild.v1" in KERNEL_CAPABILITIES
    parameters = inspect.signature(embedding.execute_embedding_rebuild).parameters
    assert set(parameters) == {"plan", "confirmation"}


def test_budget_pause_happens_before_public_provider_factory_is_constructed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _configured(tmp_path)
    _seed_r2(config)
    inspection = embedding.inspect_embedding_rebuild(config)
    plan = embedding.plan_embedding_rebuild(inspection)
    called = False

    def unexpected_provider_factory(_config: Config):
        nonlocal called
        called = True
        raise AssertionError("budget pause must not construct a Provider")

    monkeypatch.setattr(embedding, "create_embedder", unexpected_provider_factory)

    with pytest.raises(PKVRuntimeError) as paused:
        embedding.execute_embedding_rebuild(
            plan,
            embedding.confirm_embedding_rebuild(
                plan,
                approved=True,
                allow_network=True,
            ),
        )

    assert paused.value.code is ErrorCode.EMBEDDING_BUDGET_PAUSED
    assert called is False
    operations = EmbeddingRebuildLedger(config.layout).read_operations()
    assert len(operations) == 1
    assert operations[0].state is EmbeddingRebuildLedgerState.BUDGET_PAUSED
