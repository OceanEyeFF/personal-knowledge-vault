"""Runtime-cache and Config-snapshot contracts for the jieba integration."""

from __future__ import annotations

import marshal
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.utils.config as config_module
import src.utils.text_utils as text_utils
from src.application import KnowledgeApplication
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import has_active_write_lease
from src.utils.config import Config
from src.utils.text_utils import TextProcessor, preserve_jieba_global_state


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _FakeJieba:
    """Small deterministic tokenizer seam; avoids mutating jieba's real global."""

    def __init__(self, *, initialized: bool = False) -> None:
        self.dt = SimpleNamespace(
            initialized=initialized,
            tmp_dir=None,
            cache_file=None,
            dictionary=None,
            FREQ={"测试": 7},
            total=7,
            user_word_tag_tab={},
        )
        self.finalseg = SimpleNamespace(Force_Split_Words=set())
        self.initialize_calls = 0
        self.loaded_userdicts: list[str] = []
        self.on_initialize = None

    def initialize(self) -> None:
        self.initialize_calls += 1
        if self.on_initialize is not None:
            self.on_initialize()
        assert self.dt.tmp_dir is not None
        cache_path = Path(self.dt.tmp_dir) / "jieba.cache"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(marshal.dumps((self.dt.FREQ, self.dt.total)))
        self.dt.initialized = True

    def load_userdict(self, path: str) -> None:
        self.loaded_userdicts.append(path)


def _config(tmp_path: Path, name: str) -> Config:
    layout = RuntimeLayout.resolve(
        resources_root=_PROJECT_ROOT,
        user_data_root=tmp_path / name,
        environment={},
    )
    return Config(layout=layout)


def test_fresh_explicit_reader_fails_before_jieba_initialization_or_cache_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A read on an uninitialized B root must not repair its tokenizer cache."""

    config_b = _config(tmp_path, "b")
    fake_jieba = _FakeJieba()
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    with pytest.raises(PKVRuntimeError) as captured:
        TextProcessor(runtime_config=config_b)

    assert captured.value.code is ErrorCode.REPAIR_REQUIRED
    assert captured.value.stage == "tokenizer_cache"
    assert fake_jieba.initialize_calls == 0
    assert fake_jieba.loaded_userdicts == []
    assert fake_jieba.dt.tmp_dir is None
    assert not (config_b.layout.tmp_dir / "jieba.cache").exists()
    assert not config_b.data_root.exists()


def test_explicit_cache_initialization_requires_the_root_writer_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_b = _config(tmp_path, "b")
    fake_jieba = _FakeJieba()
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    with pytest.raises(PKVRuntimeError) as captured:
        TextProcessor(runtime_config=config_b, initialize_cache=True)

    assert captured.value.code is ErrorCode.REPAIR_REQUIRED
    assert captured.value.stage == "tokenizer_cache"
    assert fake_jieba.initialize_calls == 0
    assert not (config_b.layout.tmp_dir / "jieba.cache").exists()


def test_bootstrap_initializes_jieba_inside_the_root_writer_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_b = _config(tmp_path, "b")
    fake_jieba = _FakeJieba()
    observed_lease_states: list[bool] = []
    fake_jieba.on_initialize = lambda: observed_lease_states.append(
        has_active_write_lease(config_b.layout)
    )
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    bootstrap_runtime(config_b, recover_interrupted=False)

    assert observed_lease_states == [True]
    assert fake_jieba.initialize_calls == 1
    assert (config_b.layout.tmp_dir / "jieba.cache").is_file()
    assert fake_jieba.loaded_userdicts == [str(config_b.layout.custom_dict_path)]


def test_temporary_workspace_restores_jieba_global_state_before_root_teardown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A short-lived root cannot leak its tokenizer cache path into later work."""

    config = _config(tmp_path, "temporary")
    fake_jieba = _FakeJieba()
    fake_jieba.dt.tmp_dir = "stable-cache-root"
    fake_jieba.dt.cache_file = "stable.cache"
    fake_jieba.dt.dictionary = "stable.dictionary"
    fake_jieba.dt.user_word_tag_tab["稳定词"] = "nz"
    fake_jieba.finalseg.Force_Split_Words.add("稳定词")
    original_frequency = fake_jieba.dt.FREQ
    original_tags = fake_jieba.dt.user_word_tag_tab
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    with preserve_jieba_global_state():
        bootstrap_runtime(config, recover_interrupted=False)
        assert fake_jieba.dt.tmp_dir == str(config.layout.tmp_dir)
        assert (config.layout.tmp_dir / "jieba.cache").is_file()
        # Model the mutable state that ``load_userdict`` / jieba itself owns;
        # the temporary scope must restore it even when a tokenizer replaces
        # or mutates its process-global dictionaries.
        fake_jieba.dt.FREQ = {"临时词": 3}
        fake_jieba.dt.total = 3
        fake_jieba.dt.user_word_tag_tab = {"临时词": "n"}
        fake_jieba.finalseg.Force_Split_Words.add("临时词")
        assert "临时词" in fake_jieba.dt.FREQ
        assert "临时词" in fake_jieba.dt.user_word_tag_tab
        assert "临时词" in fake_jieba.finalseg.Force_Split_Words

    # The fixture/verification owner can now tear down ``config.data_root``:
    # jieba no longer retains any path or custom-dictionary state from it.
    assert fake_jieba.dt.initialized is False
    assert fake_jieba.dt.tmp_dir == "stable-cache-root"
    assert fake_jieba.dt.cache_file == "stable.cache"
    assert fake_jieba.dt.dictionary == "stable.dictionary"
    assert fake_jieba.dt.FREQ is original_frequency
    assert fake_jieba.dt.FREQ == {"测试": 7}
    assert fake_jieba.dt.total == 7
    assert fake_jieba.dt.user_word_tag_tab is original_tags
    assert fake_jieba.dt.user_word_tag_tab == {"稳定词": "nz"}
    assert fake_jieba.finalseg.Force_Split_Words == {"稳定词"}


def test_second_config_requires_and_bootstrap_materializes_its_own_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An initialized global tokenizer cannot make Config B skip B's setup."""

    config_a = _config(tmp_path, "a")
    config_b = _config(tmp_path, "b")
    fake_jieba = _FakeJieba(initialized=True)
    fake_jieba.dt.tmp_dir = str(config_a.layout.tmp_dir)
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    with pytest.raises(PKVRuntimeError) as captured:
        TextProcessor(runtime_config=config_b)
    assert captured.value.code is ErrorCode.REPAIR_REQUIRED
    assert fake_jieba.initialize_calls == 0
    assert not (config_b.layout.tmp_dir / "jieba.cache").exists()
    assert fake_jieba.dt.tmp_dir == str(config_a.layout.tmp_dir)

    bootstrap_runtime(config_b, recover_interrupted=False)

    cache_path = config_b.layout.tmp_dir / "jieba.cache"
    assert fake_jieba.initialize_calls == 0
    assert cache_path.is_file()
    assert marshal.loads(cache_path.read_bytes()) == (fake_jieba.dt.FREQ, fake_jieba.dt.total)
    assert fake_jieba.dt.tmp_dir == str(config_b.layout.tmp_dir)

    reader = TextProcessor(runtime_config=config_b)
    assert reader._runtime_config is config_b
    assert fake_jieba.loaded_userdicts == [
        str(config_b.layout.custom_dict_path),
        str(config_b.layout.custom_dict_path),
    ]
    assert fake_jieba.dt.tmp_dir == str(config_b.layout.tmp_dir)


def test_explicit_b_reader_rebinds_initialized_global_tmp_dir_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A valid B reader must not retain Config A's process-global cache target."""

    config_a = _config(tmp_path, "a")
    config_b = _config(tmp_path, "b")
    config_b.layout.ensure_user_directories()
    cache_path = config_b.layout.tmp_dir / "jieba.cache"
    cache_payload = marshal.dumps(({"B": 1}, 1))
    cache_path.write_bytes(cache_payload)
    fake_jieba = _FakeJieba(initialized=True)
    fake_jieba.dt.tmp_dir = str(config_a.layout.tmp_dir)
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)

    TextProcessor(runtime_config=config_b)

    assert fake_jieba.initialize_calls == 0
    assert cache_path.read_bytes() == cache_payload
    assert fake_jieba.dt.tmp_dir == str(config_b.layout.tmp_dir)


def test_application_config_b_paths_never_fall_back_to_global_get_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Retrieval, SQLite and relation services all retain B's tokenizer owner."""

    config_b = _config(tmp_path, "b")
    fake_jieba = _FakeJieba()
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)
    bootstrap_runtime(config_b, recover_interrupted=False)

    def reject_global_config() -> Config:
        raise AssertionError("explicit Application Config B must not consult global Config A")

    monkeypatch.setattr(config_module, "get_config", reject_global_config)
    app = KnowledgeApplication(config_b)

    sqlite_tokenizer = app.sqlite_store.text_processor
    bm25_tokenizer = app.bm25_retriever.text_processor
    router = app.query_router()
    evidence_tokenizer = app.evidence_collection_service.text_processor
    exploration_tokenizer = app.exploration_service.text_processor

    assert sqlite_tokenizer._runtime_config is config_b
    assert bm25_tokenizer._runtime_config is config_b
    assert router.text_processor._runtime_config is config_b
    assert router.hybrid_retriever.bm25_retriever.text_processor._runtime_config is config_b
    assert evidence_tokenizer._runtime_config is config_b
    assert exploration_tokenizer._runtime_config is config_b


def test_direct_legacy_text_processor_still_uses_global_config_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The old utility entry remains usable while product paths inject Config."""

    config_a = _config(tmp_path, "a")
    fake_jieba = _FakeJieba()
    monkeypatch.setattr(text_utils, "jieba", fake_jieba)
    monkeypatch.setattr(config_module, "get_config", lambda: config_a)

    processor = TextProcessor()

    assert processor._runtime_config is config_a
    assert fake_jieba.initialize_calls == 1
    assert (config_a.layout.tmp_dir / "jieba.cache").is_file()
    assert fake_jieba.loaded_userdicts == [str(config_a.layout.custom_dict_path)]
