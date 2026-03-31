"""
Embedding 维度配置持久化测试
"""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import Config


def test_config_persists_runtime_embedding_dim(tmp_path: Path, monkeypatch):
    """auto 模式下解析出的维度应写入本地缓存，并在新进程中复用。"""
    runtime_data_dir = tmp_path / "data"

    monkeypatch.setenv("DATA_DIR", str(runtime_data_dir))
    monkeypatch.setenv("OPENAI_EMBEDDING_DIM", "auto")

    config = Config()

    assert config.embedding_dim_is_auto is True
    assert config.embedding_dim is None

    config.set_runtime_embedding_dim(2560)

    assert config.embedding_dim == 2560
    assert config.runtime_embedding_dim_path.exists()

    reloaded_config = Config()

    assert reloaded_config.embedding_dim_is_auto is True
    assert reloaded_config.embedding_dim == 2560
