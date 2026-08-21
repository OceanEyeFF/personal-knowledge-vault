"""verify_setup 隔离性测试。"""

import os
from pathlib import Path
from types import SimpleNamespace

import src.utils.config as config_module
import src.utils.text_utils as text_utils
from src.utils import verify_setup


class _VerifyJieba:
    """Small global-tokenizer seam for verifier cleanup coverage."""

    def __init__(self) -> None:
        self.dt = SimpleNamespace(
            initialized=True,
            tmp_dir="stable-cache-root",
            cache_file="stable.cache",
            dictionary="stable.dictionary",
            FREQ={"稳定词": 9},
            total=9,
            user_word_tag_tab={"稳定词": "nz"},
        )
        self.finalseg = SimpleNamespace(Force_Split_Words={"稳定词"})

    def load_userdict(self, _path: str) -> None:
        self.dt.FREQ["临时词"] = 3
        self.dt.total += 3
        self.dt.user_word_tag_tab["临时词"] = "n"
        self.finalseg.Force_Split_Words.add("临时词")

    @staticmethod
    def cut(text: str):
        return list(text)


def test_verify_setup_main_isolates_paths_and_restores_state(monkeypatch):
    """六个运行路径和 Config 单例必须仅在验证期间切换。"""
    original_config = object()
    monkeypatch.setattr(config_module, "_config_instance", original_config)
    fake_jieba = _VerifyJieba()
    original_frequency = fake_jieba.dt.FREQ
    original_tags = fake_jieba.dt.user_word_tag_tab
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    previous_env = {
        "PKV_DATA_ROOT": "previous-formal-data",
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
    base_config_path = Path(__file__).parents[2] / "config" / "config.yaml"
    isolated_config: config_module.Config | None = None

    def get_auto_dim_config():
        nonlocal isolated_config
        if isolated_config is None:
            isolated_config = config_module.Config(str(base_config_path))
            isolated_config._config.setdefault("ai", {}).setdefault(
                "embedding", {}
            )["dim"] = "auto"
        config_module._config_instance = isolated_config
        return isolated_config

    monkeypatch.setattr(verify_setup, "get_config", get_auto_dim_config)
    monkeypatch.setattr(verify_setup, "test_logger", lambda **_kwargs: None)

    shutdown_calls = []

    def tracked_shutdown():
        shutdown_calls.append(True)

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
        assert Path(os.environ["PKV_DATA_ROOT"]).resolve() == root
        assert active_config.data_dir == expected_paths["DATA_DIR"]
        assert active_config.db_path == expected_paths["DB_PATH"]
        assert active_config.vault_dir == expected_paths["VAULT_DIR"]
        assert active_config.vector_index_dir == expected_paths["VECTOR_DIR"]
        assert active_config.log_dir == expected_paths["LOG_DIR"]
        assert active_config.tmp_dir == expected_paths["TMP_DIR"]
        assert index_dir == expected_paths["VECTOR_DIR"]
        assert dim is None
        assert (root / "tmp" / "jieba.cache").is_file()

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
    # The temporary workspace has been removed, so no process-global tokenizer
    # state may retain its ``tmp`` directory or custom dictionary entries.
    assert fake_jieba.dt.initialized is True
    assert fake_jieba.dt.tmp_dir == "stable-cache-root"
    assert fake_jieba.dt.cache_file == "stable.cache"
    assert fake_jieba.dt.dictionary == "stable.dictionary"
    assert fake_jieba.dt.FREQ is original_frequency
    assert fake_jieba.dt.FREQ == {"稳定词": 9}
    assert fake_jieba.dt.total == 9
    assert fake_jieba.dt.user_word_tag_tab is original_tags
    assert fake_jieba.dt.user_word_tag_tab == {"稳定词": "nz"}
    assert fake_jieba.finalseg.Force_Split_Words == {"稳定词"}
    for key, previous_value in previous_env.items():
        if previous_value is None:
            assert key not in os.environ
        else:
            assert os.environ[key] == previous_value
