"""verify_setup 隔离性测试。"""

import logging
import os
from pathlib import Path

import src.utils.config as config_module
from src.utils import verify_setup


def test_verify_setup_main_isolates_paths_and_restores_state(monkeypatch):
    """六个运行路径和 Config 单例必须仅在验证期间切换。"""
    original_config = object()
    monkeypatch.setattr(config_module, "_config_instance", original_config)

    previous_env = {
        "DATA_DIR": "previous-data",
        "DB_PATH": None,
        "VAULT_DIR": None,
        "VECTOR_DIR": None,
        "LOG_DIR": "previous-logs",
        "TMP_DIR": None,
    }
    for key, value in previous_env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    real_get_config = config_module.get_config

    def get_auto_dim_config():
        config = real_get_config()
        config._config.setdefault("ai", {}).setdefault("embedding", {})["dim"] = (
            "auto"
        )
        return config

    monkeypatch.setattr(verify_setup, "get_config", get_auto_dim_config)
    monkeypatch.setattr(verify_setup, "test_logger", lambda **_kwargs: None)

    shutdown_calls = []
    real_shutdown = logging.shutdown

    def tracked_shutdown():
        shutdown_calls.append(True)
        real_shutdown()

    monkeypatch.setattr(verify_setup.logging, "shutdown", tracked_shutdown)

    captured: dict[str, object] = {}

    def fake_vector_test(index_dir: Path | None = None, dim: int | None = None):
        active_config = real_get_config()
        root = Path(os.environ["DATA_DIR"]).resolve()
        expected_paths = {
            "DATA_DIR": root,
            "DB_PATH": root / "db" / "verify.db",
            "VAULT_DIR": root / "vault",
            "VECTOR_DIR": root / "vectors",
            "LOG_DIR": root / "logs",
            "TMP_DIR": root / "tmp",
        }

        assert all(Path(os.environ[key]).resolve() == path for key, path in expected_paths.items())
        assert active_config.data_dir == expected_paths["DATA_DIR"]
        assert active_config.db_path == expected_paths["DB_PATH"]
        assert active_config.vault_dir == expected_paths["VAULT_DIR"]
        assert active_config.vector_index_dir == expected_paths["VECTOR_DIR"]
        assert active_config.log_dir == expected_paths["LOG_DIR"]
        assert active_config.tmp_dir == expected_paths["TMP_DIR"]
        assert index_dir == expected_paths["VECTOR_DIR"]
        assert dim is None

        # 模拟 OpenAIClient 在 auto 模式下锁定真实维度；缓存必须仍在沙箱内。
        active_config.set_runtime_embedding_dim(8)
        cache_path = active_config.runtime_embedding_dim_path
        assert cache_path == root / "runtime" / "embedding_dim.json"
        assert cache_path.exists()
        captured.update(root=root, config=active_config)

    monkeypatch.setattr(verify_setup, "test_vector_store", fake_vector_test)

    verify_setup.main()

    assert captured["config"] is not original_config
    assert config_module._config_instance is original_config
    assert shutdown_calls == [True]
    assert not Path(captured["root"]).exists()
    for key, previous_value in previous_env.items():
        if previous_value is None:
            assert key not in os.environ
        else:
            assert os.environ[key] == previous_value
