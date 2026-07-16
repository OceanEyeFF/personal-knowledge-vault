"""纯 YAML 配置与 Embedding 维度持久化测试。"""

from pathlib import Path

import yaml

from src.utils.config import Config, set_yaml_config_value


def _write_config(
    path: Path,
    data_dir: Path,
    llm_model: str = "yaml-chat",
    embd_model: str = "yaml-embedding",
    embd_dim: int | str = 1536,
) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "storage": {
                    "data_dir": str(data_dir),
                    "db_path": str(data_dir / "db" / "knowledge_vault.db"),
                },
                "ai": {
                    "llm": {
                        "api_key": "",
                        "base_url": "https://llm.example.com/v1",
                        "model": llm_model,
                    },
                    "embedding": {
                        "api_key": "",
                        "base_url": "https://embd.example.com/v1",
                        "model": embd_model,
                        "dim": embd_dim,
                    },
                },
                "logging": {"level": "INFO"},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_local_yaml_recursively_overrides_base_config(tmp_path: Path) -> None:
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    _write_config(base_path, tmp_path / "data")
    local_path.write_text(
        """
ai:
  llm:
    api_key: local-llm-key
    model: local-chat
  embedding:
    api_key: local-embedding-key
    dim: auto
""",
        encoding="utf-8",
    )

    config = Config(str(base_path), str(local_path))

    assert config.llm_api_key == "local-llm-key"
    assert config.llm_model == "local-chat"
    assert config.llm_base_url == "https://llm.example.com/v1"
    assert config.embd_api_key == "local-embedding-key"
    assert config.embedding_dim_is_auto is True


def test_environment_variables_do_not_override_yaml(
    tmp_path: Path, monkeypatch
) -> None:
    base_path = tmp_path / "config.yaml"
    _write_config(base_path, tmp_path / "data", llm_model="yaml-chat")
    monkeypatch.setenv("PKV_LLM_MODEL", "env-chat")
    monkeypatch.setenv("PKV_EMBD_MODEL", "env-embedding")
    monkeypatch.setenv("PKV_LLM_API_KEY", "env-secret")

    config = Config(str(base_path))

    assert config.llm_model == "yaml-chat"
    assert config.embd_model == "yaml-embedding"
    assert config.llm_api_key is None or config.llm_api_key == ""


def test_set_yaml_config_value_preserves_sibling_values(tmp_path: Path) -> None:
    local_path = tmp_path / "local.yaml"
    set_yaml_config_value(local_path, "ai.llm.model", "first-model")
    set_yaml_config_value(local_path, "ai.llm.api_key", "secret")

    data = yaml.safe_load(local_path.read_text(encoding="utf-8"))

    assert data["ai"]["llm"]["model"] == "first-model"
    assert data["ai"]["llm"]["api_key"] == "secret"


def test_config_persists_runtime_embedding_dim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_dim="auto")

    config = Config(str(base_path))
    assert config.embedding_dim is None

    config.set_runtime_embedding_dim(2560)

    reloaded_config = Config(str(base_path))
    assert reloaded_config.embedding_dim == 2560
    assert reloaded_config.runtime_embedding_dim_path == data_dir / "runtime" / "embedding_dim.json"


def test_config_invalidates_runtime_dim_when_model_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_model="model-a", embd_dim="auto")
    config = Config(str(base_path))
    config.set_runtime_embedding_dim(2560)

    _write_config(base_path, data_dir, embd_model="model-b", embd_dim="auto")

    assert Config(str(base_path)).embedding_dim is None


def test_config_invalidates_runtime_dim_when_base_url_changes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base_path = tmp_path / "config.yaml"
    local_path = tmp_path / "local.yaml"
    data_dir = tmp_path / "data"
    _write_config(base_path, data_dir, embd_dim="auto")
    set_yaml_config_value(local_path, "ai.embedding.base_url", "https://one.example/v1")
    config = Config(str(base_path), str(local_path))
    config.set_runtime_embedding_dim(1536)

    set_yaml_config_value(local_path, "ai.embedding.base_url", "https://two.example/v1")

    assert Config(str(base_path), str(local_path)).embedding_dim is None
