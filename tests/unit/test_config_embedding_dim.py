"""
Embedding 维度配置持久化测试
"""

from pathlib import Path
from unittest.mock import patch

from src.utils.config import Config


def _write_config(
    path: Path,
    llm_model: str = "yaml-chat",
    embd_model: str = "yaml-embedding",
    embd_dim: int | str = 1536,
) -> None:
    path.write_text(
        f"""
storage:
  db_path: ".data/db/knowledge_vault.db"
ai:
  llm:
    base_url: "https://llm.example.com/v1"
    model: "{llm_model}"
  embedding:
    base_url: "https://embd.example.com/v1"
    model: "{embd_model}"
    dim: {embd_dim}
logging:
  level: "INFO"
""",
        encoding="utf-8",
    )


def test_config_persists_runtime_embedding_dim(tmp_path: Path, monkeypatch):
    """auto 模式下解析出的维度应写入本地缓存，并在新进程中复用。"""
    runtime_data_dir = tmp_path / "data"

    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    monkeypatch.setenv("PKV_EMBD_DIM", "auto")

    config = Config()

    assert config.embedding_dim_is_auto is True
    assert config.embedding_dim is None

    config.set_runtime_embedding_dim(2560)

    assert config.embedding_dim == 2560
    assert config.runtime_embedding_dim_path.exists()

    reloaded_config = Config()

    assert reloaded_config.embedding_dim_is_auto is True
    assert reloaded_config.embedding_dim == 2560


def test_config_invalidates_runtime_embedding_dim_when_model_changes(
    tmp_path: Path, monkeypatch
):
    runtime_data_dir = tmp_path / "data"

    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    monkeypatch.setenv("PKV_EMBD_DIM", "auto")
    monkeypatch.setenv("PKV_EMBD_MODEL", "text-embedding-3-small")

    config = Config()
    config.set_runtime_embedding_dim(2560)

    monkeypatch.setenv("PKV_EMBD_MODEL", "text-embedding-3-large")

    reloaded_config = Config()

    assert reloaded_config.embedding_dim_is_auto is True
    assert reloaded_config.embedding_dim is None


def test_config_invalidates_runtime_embedding_dim_when_base_url_changes(
    tmp_path: Path, monkeypatch
):
    runtime_data_dir = tmp_path / "data"

    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    monkeypatch.setenv("PKV_EMBD_DIM", "auto")
    monkeypatch.setenv("PKV_EMBD_BASE_URL", "https://api.openai.com/v1")

    config = Config()
    config.set_runtime_embedding_dim(1536)

    monkeypatch.setenv("PKV_EMBD_BASE_URL", "https://proxy.example.com/v1")

    reloaded_config = Config()

    assert reloaded_config.embedding_dim_is_auto is True
    assert reloaded_config.embedding_dim is None


def test_config_llm_model_uses_yaml_when_env_absent(tmp_path: Path, monkeypatch):
    """未设置环境变量时，LLM 模型应来自 config.yaml。"""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, llm_model="yaml-reasoner")
    monkeypatch.delenv("PKV_LLM_MODEL", raising=False)

    with patch("src.utils.config.load_dotenv"):
        config = Config(str(config_path))

    assert config.llm_model == "yaml-reasoner"
    assert config.deepseek_model == "yaml-reasoner"


def test_config_llm_model_env_overrides_yaml(tmp_path: Path, monkeypatch):
    """PKV_LLM_MODEL 应覆盖 config.yaml。"""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, llm_model="yaml-reasoner")
    monkeypatch.setenv("PKV_LLM_MODEL", "pkv-chat")

    with patch("src.utils.config.load_dotenv"):
        config = Config(str(config_path))

    assert config.llm_model == "pkv-chat"
    assert config.deepseek_model == "pkv-chat"


def test_config_llm_ignores_legacy_model_env(tmp_path: Path, monkeypatch):
    """旧 LLM 环境变量不再参与配置解析。"""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, llm_model="yaml-reasoner")
    monkeypatch.delenv("PKV_LLM_MODEL", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "legacy-chat")
    monkeypatch.setenv("SUMMARY_MODEL", "summary-chat")

    with patch("src.utils.config.load_dotenv"):
        config = Config(str(config_path))

    assert config.llm_model == "yaml-reasoner"
    assert config.deepseek_model == "yaml-reasoner"


def test_config_embedding_uses_pkv_embd_env_over_yaml(tmp_path: Path, monkeypatch):
    """PKV_EMBD_* 应覆盖 config.yaml。"""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, embd_model="yaml-embedding")
    monkeypatch.setenv("PKV_EMBD_BASE_URL", "https://pkv-embd.example.com/v1")
    monkeypatch.setenv("PKV_EMBD_API_KEY", "pkv-embd-key")
    monkeypatch.setenv("PKV_EMBD_MODEL", "pkv-embedding")
    monkeypatch.setenv("PKV_EMBD_DIM", "auto")

    with patch("src.utils.config.load_dotenv"):
        config = Config(str(config_path))

    assert config.embd_base_url == "https://pkv-embd.example.com/v1"
    assert config.embd_api_key == "pkv-embd-key"
    assert config.embd_model == "pkv-embedding"
    assert config.openai_base_url == "https://pkv-embd.example.com/v1"
    assert config.openai_api_key == "pkv-embd-key"
    assert config.openai_embedding_model == "pkv-embedding"
    assert config.embedding_dim_is_auto is True


def test_config_llm_uses_pkv_env_over_yaml(tmp_path: Path, monkeypatch):
    """PKV_LLM_* 应覆盖 config.yaml。"""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, llm_model="yaml-chat")
    monkeypatch.setenv("PKV_LLM_BASE_URL", "https://pkv-llm.example.com/v1")
    monkeypatch.setenv("PKV_LLM_API_KEY", "pkv-llm-key")
    monkeypatch.setenv("PKV_LLM_MODEL", "pkv-chat")

    with patch("src.utils.config.load_dotenv"):
        config = Config(str(config_path))

    assert config.llm_base_url == "https://pkv-llm.example.com/v1"
    assert config.llm_api_key == "pkv-llm-key"
    assert config.llm_model == "pkv-chat"
    assert config.deepseek_base_url == "https://pkv-llm.example.com/v1"
    assert config.deepseek_api_key == "pkv-llm-key"
    assert config.deepseek_model == "pkv-chat"


def test_config_embedding_ignores_legacy_env(tmp_path: Path, monkeypatch):
    """旧 Embedding 环境变量不再参与配置解析。"""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, embd_model="yaml-embedding", embd_dim=1536)
    monkeypatch.delenv("PKV_EMBD_BASE_URL", raising=False)
    monkeypatch.delenv("PKV_EMBD_API_KEY", raising=False)
    monkeypatch.delenv("PKV_EMBD_MODEL", raising=False)
    monkeypatch.delenv("PKV_EMBD_DIM", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://legacy-openai.example.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-key")
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "legacy-embedding")
    monkeypatch.setenv("OPENAI_EMBEDDING_DIM", "auto")

    with patch("src.utils.config.load_dotenv"):
        config = Config(str(config_path))

    assert config.embd_base_url == "https://embd.example.com/v1"
    assert config.embd_api_key is None
    assert config.embd_model == "yaml-embedding"
    assert config.embedding_dim == 1536
