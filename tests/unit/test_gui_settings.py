"""SettingsView pytest-qt 单元测试。

覆盖 M11 验收标准:
- SettingsView UI 结构（API 密钥输入、主题选择、检索策略、保存按钮）
- SettingsViewModel 设置读写逻辑
- 密码模式切换
- 主题变更信号

测试策略：Mock SettingsViewModel 的依赖（Config / .env），
验证 UI 组件结构和交互逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PySide6.QtWidgets import QComboBox, QLineEdit, QPushButton

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Mock 数据
# ============================================================

MOCK_SETTINGS = {
    "deepseek_api_key": "sk-test-deepseek",
    "deepseek_base_url": "https://api.deepseek.com",
    "openai_api_key": "sk-test-openai",
    "openai_base_url": "https://api.openai.com/v1",
    "theme": "",
    "search_strategy": "auto",
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
    config.deepseek_api_key = MOCK_SETTINGS["deepseek_api_key"]
    config.deepseek_base_url = MOCK_SETTINGS["deepseek_base_url"]
    config.openai_api_key = MOCK_SETTINGS["openai_api_key"]
    config.openai_base_url = MOCK_SETTINGS["openai_base_url"]
    config.db_path = ".data-test/db/test.db"
    config.vault_dir = ".data-test/vault"
    config.vector_index_dir = ".data-test/vectors"
    config.get.return_value = "auto"
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
        """包含 DeepSeek 和 OpenAI 的 API Key 输入框。"""
        assert settings_view._deepseek_key_input is not None
        assert isinstance(settings_view._deepseek_key_input, QLineEdit)
        assert settings_view._openai_key_input is not None
        assert isinstance(settings_view._openai_key_input, QLineEdit)

    def test_api_key_password_mode(self, settings_view):
        """API Key 输入框默认为密码模式。"""
        assert settings_view._deepseek_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]
        assert settings_view._openai_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]

    def test_has_theme_combo(self, settings_view):
        """包含主题选择下拉框。"""
        assert settings_view._theme_combo is not None
        assert isinstance(settings_view._theme_combo, QComboBox)
        assert settings_view._theme_combo.count() == 2  # 明亮 / 暗色

    def test_has_strategy_combo(self, settings_view):
        """包含检索策略下拉框。"""
        assert settings_view._strategy_combo is not None
        assert isinstance(settings_view._strategy_combo, QComboBox)
        assert settings_view._strategy_combo.count() == 4  # 自动/BM25/向量/混合

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
        assert settings_view._deepseek_url_input is not None
        assert settings_view._openai_url_input is not None

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

    def test_deepseek_key_loaded(self, settings_view):
        """DeepSeek API Key 已填充。"""
        assert settings_view._deepseek_key_input.text() == "sk-test-deepseek"

    def test_openai_key_loaded(self, settings_view):
        """OpenAI API Key 已填充。"""
        assert settings_view._openai_key_input.text() == "sk-test-openai"

    def test_deepseek_url_loaded(self, settings_view):
        """DeepSeek Base URL 已填充。"""
        assert settings_view._deepseek_url_input.text() == "https://api.deepseek.com"

    def test_openai_url_loaded(self, settings_view):
        """OpenAI Base URL 已填充。"""
        assert settings_view._openai_url_input.text() == "https://api.openai.com/v1"

    def test_strategy_default_is_auto(self, settings_view):
        """默认检索策略为"自动"。"""
        assert settings_view._strategy_combo.currentIndex() == 0  # "自动"

    def test_theme_combo_defaults_to_light(self, settings_view):
        """主题下拉框默认选中"明亮"。"""
        assert settings_view._theme_combo.currentIndex() == 0  # "明亮"


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
            assert "deepseek_api_key" in settings
            assert "openai_api_key" in settings
            assert "search_strategy" in settings

    def test_save_writes_env_file(self, tmp_path, mock_config):
        """save_settings 写入 .env 文件。"""
        env_file = tmp_path / ".env"
        env_file.write_text("# test\nDEEPSEEK_API_KEY=old-key\n", encoding="utf-8")

        with patch("src.gui.viewmodels.settings_viewmodel.get_config", return_value=mock_config):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel
            vm = SettingsViewModel()

            # Mock _find_env_file 返回临时 .env 路径
            with patch.object(vm, "_find_env_file", return_value=env_file):
                # Mock 重置 config 单例
                with patch("src.gui.viewmodels.settings_viewmodel.config_module", create=True):
                    import src.utils.config as config_mod
                    original = getattr(config_mod, "_config_instance", None)
                    try:
                        vm.save_settings({
                            "deepseek_api_key": "sk-new-key",
                            "openai_api_key": "sk-openai-new",
                        })
                    finally:
                        config_mod._config_instance = original

            # 验证文件内容已更新
            content = env_file.read_text(encoding="utf-8")
            assert "sk-new-key" in content

    def test_save_emits_success_signal(self, tmp_path, mock_config, qtbot):
        """保存成功后发射 settings_saved 信号。"""
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")

        with patch("src.gui.viewmodels.settings_viewmodel.get_config", return_value=mock_config):
            from src.gui.viewmodels.settings_viewmodel import SettingsViewModel
            vm = SettingsViewModel()

            with patch.object(vm, "_find_env_file", return_value=env_file):
                import src.utils.config as config_mod
                original = getattr(config_mod, "_config_instance", None)
                try:
                    with qtbot.waitSignal(vm.settings_saved, timeout=1000):
                        vm.save_settings({"deepseek_api_key": "test"})
                finally:
                    config_mod._config_instance = original


# ============================================================
# 密码模式切换
# ============================================================

class TestPasswordToggle:
    """验证密码显示/隐藏切换。"""

    def test_toggle_deepseek_key_visibility(self, settings_view):
        """切换 DeepSeek API Key 可见性。"""
        # 初始为 Password 模式
        assert settings_view._deepseek_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]

        # 点击切换按钮
        settings_view._deepseek_key_toggle.click()
        assert settings_view._deepseek_key_input.echoMode() == QLineEdit.Normal  # type: ignore[attr-defined]

        # 再次点击恢复
        settings_view._deepseek_key_toggle.click()
        assert settings_view._deepseek_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]

    def test_toggle_openai_key_visibility(self, settings_view):
        """切换 OpenAI API Key 可见性。"""
        settings_view._openai_key_toggle.click()
        assert settings_view._openai_key_input.echoMode() == QLineEdit.Normal  # type: ignore[attr-defined]

        settings_view._openai_key_toggle.click()
        assert settings_view._openai_key_input.echoMode() == QLineEdit.Password  # type: ignore[attr-defined]


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
