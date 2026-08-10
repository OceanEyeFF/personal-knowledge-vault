"""SettingsView pytest-qt 单元测试。

覆盖 M11 验收标准:
- SettingsView UI 结构（API 密钥输入、主题选择、检索策略、保存按钮）
- SettingsViewModel 设置读写逻辑
- 密码模式切换
- 主题变更信号

测试策略：Mock SettingsViewModel 的依赖（Config / config/local.yaml），
验证 UI 组件结构和交互逻辑。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml
from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Mock 数据
# ============================================================

MOCK_SETTINGS = {
    "llm_api_key": "test-llm-key",
    "llm_base_url": "https://llm.example.com/v1",
    "embedding_api_key": "test-embedding-key",
    "embedding_base_url": "https://embedding.example.com/v1",
    "theme": "",
}

MOCK_STATS = {
    "total_entries": 0,
    "by_source_type": [],
    "top_tags": [],
}


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_store():
    """创建 Mock SQLiteStore。"""
    store = MagicMock()
    store.get_statistics.return_value = MOCK_STATS
    store.get_all_tags_with_count.return_value = []
    store.list_entries.return_value = []
    store.count_entries.return_value = 0
    return store


@pytest.fixture
def mock_config():
    """创建 Mock Config 对象。"""
    config = MagicMock()
    config.llm_api_key = MOCK_SETTINGS["llm_api_key"]
    config.llm_base_url = MOCK_SETTINGS["llm_base_url"]
    config.llm_model = "deepseek-chat"
    config.embd_api_key = MOCK_SETTINGS["embedding_api_key"]
    config.embd_base_url = MOCK_SETTINGS["embedding_base_url"]
    config.embd_model = "text-embedding-3-small"
    config.db_path = ".data-test/db/test.db"
    config.vault_dir = ".data-test/vault"
    config.vector_index_dir = ".data-test/vectors"
    return config


@pytest.fixture
def settings_view(qtbot, mock_store, mock_config):
    """创建带有 Mock 依赖的 SettingsView。

    Mock 存储单例和 Config，使用 yield 确保 mock 上下文在测试期间活跃。
    """
    with patch("src.gui.stores.get_sqlite_store", return_value=mock_store), \
         patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()), \
         patch("src.gui.viewmodels.settings_viewmodel.get_config", return_value=mock_config), \
         patch("src.utils.config.get_config", return_value=mock_config):
        from src.gui.views.settings_view import SettingsView
        view = SettingsView()
        qtbot.addWidget(view)
        yield view


# ============================================================
# UI 结构验证
# ============================================================

class TestSettingsViewStructure:
    """验证 SettingsView UI 结构。"""

    def test_view_is_created(self, settings_view):
        """视图实例化不崩溃。"""
        assert settings_view is not None

    def test_has_api_key_fields(self, settings_view):
        """包含 LLM 和 Embedding 的 API Key 输入框。"""
        assert settings_view._llm_key_input is not None
        assert isinstance(settings_view._llm_key_input, QLineEdit)
        assert settings_view._embedding_key_input is not None
        assert isinstance(settings_view._embedding_key_input, QLineEdit)

    def test_api_key_password_mode(self, settings_view):
        """API Key 输入框默认为密码模式。"""
        assert settings_view._llm_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]
        assert settings_view._embedding_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]

    def test_has_theme_combo(self, settings_view):
        """包含主题选择下拉框。"""
        assert settings_view._theme_combo is not None
        assert isinstance(settings_view._theme_combo, QComboBox)
        assert settings_view._theme_combo.count() == 2  # 明亮 / 暗色

    def test_has_strategy_combo(self, settings_view):
        """检索策略只读显示 Developer Preview 的真实 BM25 能力。"""
        assert settings_view._strategy_combo is not None
        assert isinstance(settings_view._strategy_combo, QComboBox)
        assert settings_view._strategy_combo.count() == 1
        assert settings_view._strategy_combo.currentText() == "BM25（固定）"
        assert not settings_view._strategy_combo.isEnabled()
        public_text = " ".join([
            settings_view._strategy_combo.itemText(index)
            for index in range(settings_view._strategy_combo.count())
        ])
        public_text += " " + settings_view._strategy_description.text()
        assert "BM25" in public_text
        assert "Developer Preview" in public_text
        assert "自动" not in " ".join(
            settings_view._strategy_combo.itemText(index)
            for index in range(settings_view._strategy_combo.count())
        )
        assert "向量" not in settings_view._strategy_combo.currentText()
        assert "混合" not in settings_view._strategy_combo.currentText()

    def test_has_save_button(self, settings_view):
        """包含保存按钮。"""
        assert settings_view._save_btn is not None
        assert isinstance(settings_view._save_btn, QPushButton)

    def test_has_reset_button(self, settings_view):
        """包含重置按钮。"""
        assert settings_view._reset_btn is not None
        assert isinstance(settings_view._reset_btn, QPushButton)

    def test_has_base_url_inputs(self, settings_view):
        """包含 Base URL 输入框。"""
        assert settings_view._llm_url_input is not None
        assert settings_view._embedding_url_input is not None

    def test_has_data_path_labels(self, settings_view):
        """包含数据目录只读标签。"""
        assert settings_view._db_path_label is not None
        assert settings_view._vault_dir_label is not None
        assert settings_view._vector_dir_label is not None

    def test_has_status_label(self, settings_view):
        """包含状态消息标签。"""
        assert settings_view._status_label is not None


# ============================================================
# 设置加载与填充验证
# ============================================================

class TestSettingsViewData:
    """验证 SettingsView 数据加载。"""

    def test_llm_key_loaded(self, settings_view):
        """LLM API Key 已填充。"""
        assert settings_view._llm_key_input.text() == "test-llm-key"

    def test_embedding_key_loaded(self, settings_view):
        """Embedding API Key 已填充。"""
        assert settings_view._embedding_key_input.text() == "test-embedding-key"

    def test_llm_url_loaded(self, settings_view):
        """LLM Base URL 已填充。"""
        assert settings_view._llm_url_input.text() == "https://llm.example.com/v1"

    def test_embedding_url_loaded(self, settings_view):
        """Embedding Base URL 已填充。"""
        assert settings_view._embedding_url_input.text() == "https://embedding.example.com/v1"

    def test_endpoint_credentials_are_redacted_before_entering_line_edits(
        self, settings_view, mock_config
    ):
        """普通 Base URL 文本框不得持有 endpoint 内嵌凭据原文。"""
        mock_config.llm_base_url = (
            "https://view-user:view-pass@llm.example/v1"
            ";pwd=view-matrix;JSESSIONID=view-session"
            "?api_key=view-query&jwt=view-jwt#code=view-fragment"
        )
        mock_config.embd_base_url = (
            "https://embedding.example/v1?access_token=embedding-secret"
        )

        settings_view._load_and_fill()

        llm_display = settings_view._llm_url_input.text()
        embedding_display = settings_view._embedding_url_input.text()
        assert "llm.example" in llm_display
        assert "embedding.example" in embedding_display
        assert "已隐藏" in llm_display
        assert "已隐藏" in embedding_display
        for sentinel in (
            "view-user",
            "view-pass",
            "view-matrix",
            "view-query",
            "view-session",
            "view-jwt",
            "view-fragment",
            "embedding-secret",
        ):
            assert sentinel not in llm_display
            assert sentinel not in embedding_display

    def test_strategy_is_fixed_to_bm25(self, settings_view):
        """GUI 搜索策略固定为唯一 BM25 项。"""
        assert settings_view._strategy_combo.currentIndex() == 0
        assert settings_view._strategy_combo.currentText() == "BM25（固定）"

    def test_theme_combo_defaults_to_light(self, settings_view):
        """主题下拉框默认选中"明亮"。"""
        assert settings_view._theme_combo.currentIndex() == 0  # "明亮"

    def test_save_success_is_forwarded_to_parent(self, settings_view, qtbot):
        """ViewModel 保存成功后，视图向主窗口转发刷新通知。"""
        with qtbot.waitSignal(settings_view.settings_saved, timeout=1000):
            settings_view._vm.settings_saved.emit()


# ============================================================
# SettingsViewModel 验证
# ============================================================

class TestSettingsViewModel:
    """验证 SettingsViewModel。"""

    def test_load_settings_returns_dict(self, mock_config):
        """load_settings 返回设置字典。"""
        with patch("src.gui.viewmodels.settings_viewmodel.get_config", return_value=mock_config):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel
            vm = SettingsViewModel()
            settings = vm.load_settings()
            assert isinstance(settings, dict)
            assert "llm_api_key" in settings
            assert "embedding_api_key" in settings
            assert "search_strategy" not in settings
            mock_config.get.assert_not_called()

    def test_save_writes_local_yaml(self, tmp_path, mock_config):
        """save_settings 写入 config/local.yaml。"""
        local_file = tmp_path / "local.yaml"

        with patch("src.gui.viewmodels.settings_viewmodel.get_config", return_value=mock_config):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel
            vm = SettingsViewModel()

            with patch.object(vm, "_find_local_config_file", return_value=local_file):
                # Mock 重置 config 单例
                with patch("src.gui.viewmodels.settings_viewmodel.config_module", create=True):
                    import src.utils.config as config_mod
                    original = getattr(config_mod, "_config_instance", None)
                    try:
                        with patch(
                            "src.gui.viewmodels.settings_viewmodel.set_yaml_config_values",
                            wraps=config_mod.set_yaml_config_values,
                        ) as bulk_update:
                            vm.save_settings({
                                "llm_api_key": "new-llm-key",
                                "llm_base_url": (
                                    "https://new-llm.example.com/v1"
                                    "?region_code=north&routing_key=primary"
                                ),
                                "embedding_api_key": "new-embedding-key",
                                "embedding_base_url": "https://new-embedding.example.com/v1",
                                "search_strategy": "hybrid",
                            })
                    finally:
                        config_mod._config_instance = original

            bulk_update.assert_called_once()

            data = yaml.safe_load(local_file.read_text(encoding="utf-8"))
            assert data["ai"]["llm"]["api_key"] == "new-llm-key"
            assert data["ai"]["llm"]["base_url"] == (
                "https://new-llm.example.com/v1"
                "?region_code=north&routing_key=primary"
            )
            assert data["ai"]["embedding"]["api_key"] == "new-embedding-key"
            assert (
                data["ai"]["embedding"]["base_url"]
                == "https://new-embedding.example.com/v1"
            )
            assert "retrieval" not in data

    def test_bulk_save_failure_leaves_existing_yaml_unchanged(
        self, tmp_path, mock_config, qtbot, caplog
    ):
        """多字段写入只有一个原子提交点，底层失败时原文件不变。"""
        local_file = tmp_path / "local.yaml"
        original = b"service:\n  mode: original\n"
        local_file.write_bytes(original)

        with patch(
            "src.gui.viewmodels.settings_viewmodel.get_config",
            return_value=mock_config,
        ):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

            vm = SettingsViewModel()
            saved = MagicMock()
            vm.settings_saved.connect(saved)
            with patch.object(vm, "_find_local_config_file", return_value=local_file):
                with patch(
                    "src.gui.viewmodels.settings_viewmodel.set_yaml_config_values",
                    side_effect=OSError(
                        r"C:\private\config\local.yaml "
                        "api_key=SETTINGS-SECRET-CANARY"
                    ),
                ) as bulk_update:
                    caplog.set_level(
                        logging.ERROR,
                        logger="pkv.gui.viewmodels.settings",
                    )
                    with qtbot.waitSignal(
                        vm.error_occurred,
                        timeout=1000,
                    ) as blocker:
                        vm.save_settings(
                            {
                                "llm_api_key": "new-llm-key",
                                "embedding_api_key": "new-embedding-key",
                            }
                        )

        bulk_update.assert_called_once()
        saved.assert_not_called()
        assert local_file.read_bytes() == original
        assert blocker.args[0] == (
            "设置保存失败（错误代码：settings_save_failed）。"
            "请检查用户配置文件权限后重试。"
        )
        assert "SETTINGS-SECRET-CANARY" not in blocker.args[0]
        assert "private" not in blocker.args[0]
        assert "SETTINGS-SECRET-CANARY" not in caplog.text
        assert "private" not in caplog.text
        assert "settings_save_failed" in caplog.text
        assert "OSError" in caplog.text

    def test_save_emits_success_signal(self, tmp_path, mock_config, qtbot):
        """保存成功后发射 settings_saved 信号。"""
        local_file = tmp_path / "local.yaml"

        with patch("src.gui.viewmodels.settings_viewmodel.get_config", return_value=mock_config):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel
            vm = SettingsViewModel()

            with patch.object(vm, "_find_local_config_file", return_value=local_file):
                import src.utils.config as config_mod
                original = getattr(config_mod, "_config_instance", None)
                try:
                    with qtbot.waitSignal(vm.settings_saved, timeout=1000):
                        vm.save_settings({"llm_api_key": "test"})
                finally:
                    config_mod._config_instance = original

    def test_obsolete_search_strategy_is_ignored_without_writing(
        self, mock_config, qtbot
    ):
        """历史调用方提交 vector/hybrid 也不得写入无效配置键。"""
        with patch(
            "src.gui.viewmodels.settings_viewmodel.get_config",
            return_value=mock_config,
        ):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

            vm = SettingsViewModel()
            with patch.object(vm, "_update_local_config") as update:
                with qtbot.waitSignal(vm.settings_saved, timeout=1000):
                    vm.save_settings({"search_strategy": "vector"})

        update.assert_not_called()

    def test_unchanged_redacted_base_url_is_skipped_and_not_written(
        self, tmp_path, mock_config, qtbot
    ):
        """未编辑的脱敏展示只作 UI 快照，不得写回本机 YAML。"""
        endpoint = (
            "https://saved-user:saved-pass@llm.example/v1"
            "?api_key=saved-query"
        )
        mock_config.llm_base_url = endpoint
        local_file = tmp_path / "local.yaml"
        local_file.write_text(
            yaml.safe_dump(
                {"ai": {"llm": {"base_url": endpoint}}},
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with patch(
            "src.gui.viewmodels.settings_viewmodel.get_config",
            return_value=mock_config,
        ):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

            vm = SettingsViewModel()
            displayed = vm.load_settings()["llm_base_url"]
            with patch.object(vm, "_find_local_config_file", return_value=local_file):
                with qtbot.waitSignal(vm.settings_saved, timeout=1000):
                    vm.save_settings(
                        {
                            "llm_base_url": displayed,
                            "search_strategy": "hybrid",
                        }
                    )

        persisted = local_file.read_text(encoding="utf-8")
        data = yaml.safe_load(persisted)
        assert data["ai"]["llm"]["base_url"] == endpoint
        assert "retrieval" not in data
        assert "已隐藏" not in persisted

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://user:secret@example/v1",
            "https://example/v1;passwd=secret",
            "https://example/v1;JSESSIONID=secret",
            "https://example/v1?pwd=secret",
            "https://example/v1?jwt=secret",
            "https://example/v1#auth=secret",
            "https://example/v1#session-id=secret",
        ],
    )
    def test_save_rejects_edited_credential_base_url_without_writing(
        self, endpoint, tmp_path, mock_config, qtbot
    ):
        """GUI 编辑值含凭据时拒绝，并引导直接编辑 ignored local.yaml。"""
        local_file = tmp_path / "local.yaml"

        with patch(
            "src.gui.viewmodels.settings_viewmodel.get_config",
            return_value=mock_config,
        ):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

            vm = SettingsViewModel()
            with patch.object(vm, "_find_local_config_file", return_value=local_file):
                with qtbot.waitSignal(vm.error_occurred, timeout=1000) as blocker:
                    vm.save_settings({"llm_base_url": endpoint})

        error_message = blocker.args[0]
        assert "config/local.yaml" in error_message
        assert "直接编辑" in error_message
        assert "secret" not in error_message
        assert not local_file.exists()

    def test_save_rejects_edited_redaction_placeholder(
        self, tmp_path, mock_config, qtbot
    ):
        """用户编辑后的脱敏占位符不能被误当成真实 endpoint 持久化。"""
        mock_config.llm_base_url = "https://example/v1?api_key=original-secret"
        local_file = tmp_path / "local.yaml"

        with patch(
            "src.gui.viewmodels.settings_viewmodel.get_config",
            return_value=mock_config,
        ):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

            vm = SettingsViewModel()
            displayed = vm.load_settings()["llm_base_url"]
            edited = f"{displayed}&region=changed"
            with patch.object(vm, "_find_local_config_file", return_value=local_file):
                with qtbot.waitSignal(vm.error_occurred, timeout=1000) as blocker:
                    vm.save_settings({"llm_base_url": edited})

        assert "脱敏占位符" in blocker.args[0]
        assert "original-secret" not in blocker.args[0]
        assert not local_file.exists()

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://evil.example/v1",
            "http://localhost:8000/v1",
            "javascript:alert(1)",
            "https://",
            "https://example.com:99999/v1",
            "https://example.com/v1\r\napi_key=SETTINGS-URL-CANARY",
        ],
    )
    def test_save_rejects_provider_endpoint_that_factory_cannot_use(
        self,
        endpoint,
        mock_config,
        qtbot,
        caplog,
    ):
        """Settings and production construction share one endpoint boundary."""
        from src.gui.viewmodels.settings_viewmodel import (
            SettingsViewModel,
            _INVALID_URL_ERROR,
        )

        vm = SettingsViewModel()
        saved = MagicMock()
        vm.settings_saved.connect(saved)
        with patch.object(vm, "_update_local_config") as update:
            with qtbot.waitSignal(vm.error_occurred, timeout=1000) as blocker:
                with caplog.at_level(
                    "ERROR",
                    logger="pkv.gui.viewmodels.settings",
                ):
                    vm.save_settings({"llm_base_url": endpoint})

        update.assert_not_called()
        saved.assert_not_called()
        assert blocker.args == [_INVALID_URL_ERROR]
        assert "settings_endpoint_invalid" in caplog.text
        assert endpoint not in blocker.args[0]
        assert "SETTINGS-URL-CANARY" not in caplog.text

    def test_save_accepts_numeric_loopback_http_provider_endpoint(
        self,
        qtbot,
    ):
        """The shared provider rule retains explicit local test harnesses."""
        from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

        vm = SettingsViewModel()
        endpoint = "http://127.0.0.1:8000/v1"
        with patch.object(vm, "_update_local_config") as update:
            with qtbot.waitSignal(vm.settings_saved, timeout=1000):
                vm.save_settings({"embedding_base_url": endpoint})

        update.assert_called_once_with({"ai.embedding.base_url": endpoint})

    def test_view_submits_only_edited_base_urls(self, settings_view):
        """View 层不提交未编辑 endpoint，普通 URL 编辑仍可保存。"""
        with patch.object(settings_view._vm, "save_settings") as save_settings:
            settings_view._on_save()

        first_payload = save_settings.call_args.args[0]
        assert "llm_base_url" not in first_payload
        assert "embedding_base_url" not in first_payload
        assert "search_strategy" not in first_payload

        settings_view._llm_url_input.setText(
            "https://new-llm.example/v1?region=cn&route=primary"
        )
        with patch.object(settings_view._vm, "save_settings") as save_settings:
            settings_view._on_save()

        second_payload = save_settings.call_args.args[0]
        assert second_payload["llm_base_url"] == (
            "https://new-llm.example/v1?region=cn&route=primary"
        )
        assert "embedding_base_url" not in second_payload
        assert "search_strategy" not in second_payload

    def test_view_rejects_untrusted_error_message_canary(self, settings_view):
        """即使错误信号被污染，UI 和 View 日志也只保留固定消息。"""
        canary = (
            r"C:\private\config\local.yaml "
            "Authorization: Bearer VIEW-SECRET-CANARY"
        )

        settings_view._on_save_error(canary)

        public_text = settings_view._status_label.text()
        assert "settings_save_failed" in public_text
        assert "VIEW-SECRET-CANARY" not in public_text
        assert "private" not in public_text


# ============================================================
# 密码模式切换
# ============================================================

class TestPasswordToggle:
    """验证密码显示/隐藏切换。"""

    def test_toggle_llm_key_visibility(self, settings_view):
        """切换 LLM API Key 可见性。"""
        # 初始为 Password 模式
        assert settings_view._llm_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]

        # 点击切换按钮
        settings_view._llm_key_toggle.click()
        assert settings_view._llm_key_input.echoMode() == QLineEdit.Normal  # type: ignore[attr-defined]

        # 再次点击恢复
        settings_view._llm_key_toggle.click()
        assert settings_view._llm_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]

    def test_toggle_embedding_key_visibility(self, settings_view):
        """切换 Embedding API Key 可见性。"""
        settings_view._embedding_key_toggle.click()
        assert settings_view._embedding_key_input.echoMode() == QLineEdit.Normal  # type: ignore[attr-defined]

        settings_view._embedding_key_toggle.click()
        assert settings_view._embedding_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]


# ============================================================
# 主题变更信号
# ============================================================

class TestThemeChange:
    """验证主题变更信号。"""

    def test_theme_change_emits_signal(self, settings_view, qtbot):
        """切换主题后保存触发 theme_change_requested 信号。"""
        # 切换到暗色主题（索引 1）
        settings_view._theme_combo.setCurrentIndex(1)

        # Mock save 操作避免实际写文件
        with patch.object(settings_view._vm, "save_settings"):
            with qtbot.waitSignal(settings_view.theme_change_requested, timeout=1000) as blocker:
                settings_view._on_save()
            assert blocker.args[0] == "dark"
