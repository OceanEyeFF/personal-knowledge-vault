"""W2 provider construction contract tests."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

from src.ai.provider_factory import (
    ChatProviderSettings,
    EmbeddingProviderSettings,
    chat_settings_from_config,
    create_embedder,
    create_embedding_client,
    embedding_settings_from_config,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.storage.vector_store import VectorStore
from src.utils.config import Config


@dataclass
class _Config:
    llm_provider: str = "openai_compatible"
    llm_api_key: str = "chat-key"
    llm_base_url: str = "https://chat.example/v1"
    llm_model: str = "chat-model"
    llm_max_tokens: int = 512
    llm_temperature: float = 0.2
    llm_timeout_seconds: float = 7.5
    llm_max_retries: int = 1
    embd_provider: str = "openai_compatible"
    embd_api_key: str = "embedding-key"
    embd_base_url: str = "https://embedding.example/v1"
    embd_model: str = "embedding-model"
    embedding_dim: int = 64
    embd_timeout_seconds: float = 8.5
    embd_max_retries: int = 2


def test_embedding_client_uses_explicit_keyword_contract() -> None:
    observed = {}

    def client_type(**kwargs):
        observed.update(kwargs)
        return object()

    settings = EmbeddingProviderSettings(
        provider="openai_compatible",
        api_key="embedding-key",
        base_url="https://embedding.example/v1?api-version=1",
        model="embedding-model",
        dimensions=64,
        timeout_seconds=8.5,
        max_retries=2,
    )

    create_embedding_client(settings, client_type=client_type)

    assert observed == {
        "api_key": "embedding-key",
        "base_url": "https://embedding.example/v1?api-version=1",
        "model": "embedding-model",
        "dimensions": 64,
        "timeout": 8.5,
        "max_retries": 2,
    }


def test_real_embedding_client_consumes_only_the_frozen_snapshot() -> None:
    settings = EmbeddingProviderSettings(
        provider="openai_compatible",
        api_key="snapshot-key",
        base_url="https://embedding.example/v1",
        model="snapshot-model",
        dimensions=None,
        timeout_seconds=9.5,
        max_retries=1,
    )

    with (
        patch(
            "src.ai.openai_client.get_config",
            side_effect=AssertionError("global config must not be read"),
        ) as get_config,
        patch("src.ai.openai_client.OpenAI") as sdk_client,
    ):
        client = create_embedding_client(settings)

    get_config.assert_not_called()
    sdk_client.assert_called_once_with(
        api_key="snapshot-key",
        base_url="https://embedding.example/v1",
        timeout=9.5,
        max_retries=1,
    )
    assert client.model == "snapshot-model"
    assert client.dimensions is None
    assert client._auto_dimensions_pending is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dimensions", True),
        ("dimensions", 1.5),
        ("max_retries", True),
        ("max_retries", 1.2),
        ("timeout_seconds", float("inf")),
        ("base_url", "https://embedding.example:bad/v1"),
        ("base_url", "https://embedding.example:0/v1"),
        ("base_url", "https://embedding.example:65536/v1"),
    ],
)
def test_direct_embedding_settings_fail_before_client_construction(
    field: str,
    value,
) -> None:
    values = {
        "provider": "openai_compatible",
        "api_key": "embedding-key",
        "base_url": "https://embedding.example/v1",
        "model": "embedding-model",
        "dimensions": 64,
        "timeout_seconds": 8.5,
        "max_retries": 2,
    }
    values[field] = value
    constructed = 0

    def client_type(**kwargs):
        nonlocal constructed
        constructed += 1
        return object()

    with pytest.raises(PKVRuntimeError) as captured:
        create_embedding_client(
            EmbeddingProviderSettings(**values),
            client_type=client_type,
        )

    assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
    assert constructed == 0


def test_chat_settings_are_an_immutable_config_snapshot() -> None:
    config = _Config()

    settings = chat_settings_from_config(config)
    config.llm_model = "changed-later"

    assert settings == ChatProviderSettings(
        provider="openai_compatible",
        api_key="chat-key",
        base_url="https://chat.example/v1",
        model="chat-model",
        max_tokens=512,
        temperature=0.2,
        timeout_seconds=7.5,
        max_retries=1,
    )


def test_embedding_settings_are_an_immutable_config_snapshot() -> None:
    config = _Config()

    settings = embedding_settings_from_config(config)
    config.embd_model = "changed-later"

    assert settings == EmbeddingProviderSettings(
        provider="openai_compatible",
        api_key="embedding-key",
        base_url="https://embedding.example/v1",
        model="embedding-model",
        dimensions=64,
        timeout_seconds=8.5,
        max_retries=2,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_provider", "fake"),
        ("llm_api_key", ""),
        ("llm_base_url", "http://provider.example/v1"),
        ("llm_base_url", "https://user:secret@provider.example/v1"),
        ("llm_timeout_seconds", 0),
        ("llm_max_retries", -1),
        ("llm_max_tokens", 0),
        ("llm_temperature", 2.1),
        ("llm_max_tokens", True),
        ("llm_max_tokens", 1.5),
        ("llm_max_tokens", "invalid-number-secret"),
        ("llm_max_retries", True),
        ("llm_max_retries", 1.9),
        ("llm_max_retries", 11),
        ("llm_timeout_seconds", float("nan")),
        ("llm_timeout_seconds", float("inf")),
        ("llm_timeout_seconds", 601),
        ("llm_base_url", "https://provider.example:bad/v1"),
        ("llm_base_url", "https://provider.example:0/v1"),
        ("llm_base_url", "https://provider.example:65536/v1"),
    ],
)
def test_invalid_chat_provider_configuration_fails_closed(field: str, value) -> None:
    config = _Config()
    setattr(config, field, value)

    with pytest.raises(PKVRuntimeError) as captured:
        chat_settings_from_config(config)

    assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
    assert "invalid-number-secret" not in str(captured.value)


def test_numeric_loopback_http_is_available_to_external_artifact_harness() -> None:
    config = _Config(llm_base_url="http://127.0.0.1:43123/v1")

    assert chat_settings_from_config(config).base_url == "http://127.0.0.1:43123/v1"


@pytest.mark.parametrize("dimensions", [0, -1, True, 1.5, 65_537])
def test_invalid_embedding_dimensions_fail_closed(dimensions) -> None:
    config = _Config(embedding_dim=dimensions)

    with pytest.raises(PKVRuntimeError) as captured:
        embedding_settings_from_config(config)

    assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("embd_max_retries", True),
        ("embd_max_retries", 1.2),
        ("embd_max_retries", 11),
        ("embd_timeout_seconds", float("nan")),
        ("embd_timeout_seconds", 601),
        ("embd_base_url", "https://embedding.example:bad/v1"),
        ("embd_base_url", "https://embedding.example:0/v1"),
        ("embd_base_url", "https://embedding.example:65536/v1"),
    ],
)
def test_invalid_embedding_provider_configuration_fails_closed(
    field: str,
    value,
) -> None:
    config = _Config()
    setattr(config, field, value)

    with pytest.raises(PKVRuntimeError) as captured:
        embedding_settings_from_config(config)

    assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID


def _write_real_config(
    path: Path,
    *,
    max_tokens,
    dimensions,
    layout: RuntimeLayout | None = None,
) -> Config:
    payload = {
        "storage": {"data_dir": str(path.parent / "data")},
        "ai": {
            "llm": {
                "provider": "openai_compatible",
                "api_key": "fixture-chat-key",
                "base_url": "https://chat.example/v1",
                "model": "fixture-chat",
                "max_tokens": max_tokens,
                "temperature": 0.2,
                "timeout_seconds": 7.5,
                "max_retries": 1,
            },
            "embedding": {
                "provider": "openai_compatible",
                "api_key": "fixture-embedding-key",
                "base_url": "https://embedding.example/v1",
                "model": "fixture-embedding",
                "dim": dimensions,
                "timeout_seconds": 8.5,
                "max_retries": 2,
            },
        },
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True),
        encoding="utf-8",
    )
    return Config(config_path=str(path), layout=layout)


def _isolated_layout(tmp_path: Path, config_path: Path) -> RuntimeLayout:
    return RuntimeLayout.resolve(
        resources_root=Path(__file__).parents[2],
        user_data_root=tmp_path / "runtime-data",
        base_config_path=config_path,
        environment={},
    )


def test_auto_dimension_sink_persists_exactly_once_and_feeds_new_index(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    layout = _isolated_layout(tmp_path, config_path)
    config = _write_real_config(
        config_path,
        max_tokens=512,
        dimensions="auto",
        layout=layout,
    )
    assert config.embedding_dim is None

    response = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.1, 0.2, 0.3])],
        usage=None,
    )
    with (
        patch(
            "src.ai.openai_client.get_config",
            side_effect=AssertionError("snapshot client must not read Config"),
        ) as get_config,
        patch("src.ai.openai_client.OpenAI"),
        patch.object(
            config,
            "set_runtime_embedding_dim",
            wraps=config.set_runtime_embedding_dim,
        ) as dimension_sink,
    ):
        embedder = create_embedder(config)
        embedder.client.client.embeddings.create = Mock(return_value=response)

        first_vector = embedder.embed_document("first auto vector")
        second_vector = embedder.embed_document("second auto vector")

    get_config.assert_not_called()
    dimension_sink.assert_called_once_with(3)
    assert first_vector.shape == (3,)
    assert second_vector.shape == (3,)
    assert embedder.dim == 3
    assert config.embedding_dim == 3

    restarted = Config(config_path=str(config_path), layout=layout)
    assert restarted.embedding_dim == 3

    with patch("src.storage.vector_store.get_config", return_value=config):
        vector_store = VectorStore(
            layout.vector_index_dir,
            dim=embedder.dim,
            layout=layout,
        )
    assert vector_store.dim == 3


@pytest.mark.parametrize(
    "data",
    [
        [],
        [SimpleNamespace(index=1, embedding=[0.1, 0.2, 0.3])],
        [SimpleNamespace(index=0, embedding=[float("nan"), 0.2, 0.3])],
        [SimpleNamespace(index=0, embedding=[1e308, 0.2, 0.3])],
        [SimpleNamespace(index=0, embedding=[0.0, 0.0, 0.0])],
        [SimpleNamespace(index=0, embedding=[1e-30, 1e-30, 1e-30])],
        [SimpleNamespace(index=0, embedding=[3e38, 3e38, 3e38])],
        [SimpleNamespace(index=0, embedding=[])],
    ],
)
def test_auto_dimension_protocol_failure_never_calls_sink(
    tmp_path: Path,
    data,
) -> None:
    config_path = tmp_path / "config.yaml"
    layout = _isolated_layout(tmp_path, config_path)
    config = _write_real_config(
        config_path,
        max_tokens=512,
        dimensions="auto",
        layout=layout,
    )

    with (
        patch(
            "src.ai.openai_client.get_config",
            side_effect=AssertionError("snapshot client must not read Config"),
        ) as get_config,
        patch("src.ai.openai_client.OpenAI"),
        patch.object(
            config,
            "set_runtime_embedding_dim",
            wraps=config.set_runtime_embedding_dim,
        ) as dimension_sink,
    ):
        embedder = create_embedder(config)
        embedder.client.client.embeddings.create = Mock(
            return_value=SimpleNamespace(data=data, usage=None)
        )
        with pytest.raises(PKVRuntimeError) as captured:
            embedder.embed_document("invalid auto vector")

    assert captured.value.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert captured.value.stage == "embedding_protocol"
    get_config.assert_not_called()
    dimension_sink.assert_not_called()
    assert embedder.dim is None
    assert config.embedding_dim is None
    assert not layout.runtime_state_dir.joinpath("embedding_dim.json").exists()
    assert not layout.vector_index_dir.exists()


def test_auto_dimension_publish_failure_keeps_factory_retryable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    layout = _isolated_layout(tmp_path, config_path)
    config = _write_real_config(
        config_path,
        max_tokens=512,
        dimensions="auto",
        layout=layout,
    )
    response = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.1, 0.2, 0.3])],
        usage=None,
    )

    with (
        patch(
            "src.ai.openai_client.get_config",
            side_effect=AssertionError("snapshot client must not read Config"),
        ) as get_config,
        patch("src.ai.openai_client.OpenAI"),
        patch.object(
            config,
            "_write_runtime_embedding_payload",
            side_effect=OSError("injected runtime publish failure"),
        ),
    ):
        failed_embedder = create_embedder(config)
        failed_embedder.client.client.embeddings.create = Mock(return_value=response)
        with pytest.raises(OSError, match="injected runtime publish failure"):
            failed_embedder.embed_document("first persistence attempt")

    assert failed_embedder.dim is None
    assert failed_embedder.client._auto_dimensions_pending is True
    assert config.embedding_dim is None
    assert not layout.runtime_state_dir.joinpath("embedding_dim.json").exists()

    with (
        patch(
            "src.ai.openai_client.get_config",
            side_effect=AssertionError("snapshot client must not read Config"),
        ),
        patch("src.ai.openai_client.OpenAI"),
    ):
        retry_embedder = create_embedder(config)
        retry_embedder.client.client.embeddings.create = Mock(return_value=response)
        vector = retry_embedder.embed_document("retry persistence")

    get_config.assert_not_called()
    assert vector.shape == (3,)
    assert retry_embedder.dim == 3
    assert config.embedding_dim == 3
    assert layout.runtime_state_dir.joinpath("embedding_dim.json").is_file()


def test_concurrent_auto_clients_use_durable_dimension_cas(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    layout = _isolated_layout(tmp_path, config_path)
    config = _write_real_config(
        config_path,
        max_tokens=512,
        dimensions="auto",
        layout=layout,
    )
    sdk_clients = [Mock(), Mock()]
    barrier = Barrier(2)

    def response_for(dim: int):
        def create(**_kwargs):
            barrier.wait(timeout=5)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        index=0,
                        embedding=[0.1] * dim,
                    )
                ],
                usage=None,
            )

        return create

    sdk_clients[0].embeddings.create.side_effect = response_for(3)
    sdk_clients[1].embeddings.create.side_effect = response_for(4)

    with (
        patch(
            "src.ai.openai_client.get_config",
            side_effect=AssertionError("snapshot client must not read Config"),
        ),
        patch("src.ai.openai_client.OpenAI", side_effect=sdk_clients),
    ):
        embedders = [create_embedder(config), create_embedder(config)]

        def run(index: int):
            try:
                return ("success", embedders[index].embed_document("race"))
            except Exception as exc:
                return ("error", exc)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(run, (0, 1)))

    successes = [value for status, value in outcomes if status == "success"]
    failures = [value for status, value in outcomes if status == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    failure = failures[0]
    assert isinstance(failure, PKVRuntimeError)
    assert failure.code is ErrorCode.PROVIDER_PROTOCOL_FAILED
    assert failure.stage == "embedding_protocol"

    winner_dim = int(successes[0].shape[0])
    assert winner_dim in {3, 4}
    assert config.embedding_dim == winner_dim
    assert sorted(
        embedder.dim for embedder in embedders if embedder.dim is not None
    ) == [winner_dim]

    restarted = Config(config_path=str(config_path), layout=layout)
    assert restarted.embedding_dim == winner_dim
    with patch.object(
        config,
        "_write_runtime_embedding_payload",
        wraps=config._write_runtime_embedding_payload,
    ) as publish:
        config.set_runtime_embedding_dim(winner_dim)
    publish.assert_not_called()

    with patch("src.storage.vector_store.get_config", return_value=config):
        vector_store = VectorStore(
            layout.vector_index_dir,
            dim=winner_dim,
            layout=layout,
        )
    assert vector_store.dim == winner_dim


@pytest.mark.parametrize("raw_value", [True, 1.5, "raw-secret-number"])
def test_real_config_snapshot_rejects_raw_chat_value_before_property_coercion(
    tmp_path: Path,
    raw_value,
) -> None:
    config = _write_real_config(
        tmp_path / "config.yaml",
        max_tokens=raw_value,
        dimensions=64,
    )

    with pytest.raises(PKVRuntimeError) as captured:
        chat_settings_from_config(config)

    assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
    assert "raw-secret-number" not in str(captured.value)


@pytest.mark.parametrize("raw_value", [True, 1.5, "raw-secret-dimension"])
def test_real_config_snapshot_rejects_raw_embedding_value_before_coercion(
    tmp_path: Path,
    raw_value,
) -> None:
    config = _write_real_config(
        tmp_path / "config.yaml",
        max_tokens=512,
        dimensions=raw_value,
    )

    with pytest.raises(PKVRuntimeError) as captured:
        embedding_settings_from_config(config)

    assert captured.value.code is ErrorCode.PROVIDER_CONFIG_INVALID
    assert "raw-secret-dimension" not in str(captured.value)
