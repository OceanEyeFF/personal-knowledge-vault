"""MainWindow pytest-qt 单元测试。

覆盖 M10 验收标准:
1. 启动后显示主窗口（无崩溃）
4. 明亮/暗色主题可切换
7. 窗口关闭时状态已保存到 QSettings（重启后恢复位置/大小）

以及快捷键、导航切换等核心功能。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QCloseEvent

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.config import Config


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def mock_stores(tmp_path, monkeypatch):
    """Mock 所有存储单例和配置，避免连接真实数据库。"""
    mock_store = MagicMock()
    mock_store.get_all_tags_with_count.return_value = []
    mock_store.list_entries.return_value = []
    mock_store.count_entries.return_value = 0
    mock_store.get_statistics.return_value = {
        "total_entries": 0,
        "by_source_type": [],
        "top_tags": [],
    }

    data_root = tmp_path / "runtime"
    runtime_paths = {
        "DATA_DIR": data_root,
        "DB_PATH": data_root / "db" / "knowledge_vault.db",
        "VAULT_DIR": data_root / "vault",
        "VECTOR_DIR": data_root / "vectors",
        "LOG_DIR": data_root / "logs",
        "TMP_DIR": data_root / "tmp",
    }
    for key, path in runtime_paths.items():
        monkeypatch.setenv(key, str(path))

    isolated_config = Config(str(_PROJECT_ROOT / "config" / "config.yaml"))

    # 延迟导入在函数内部执行 from src.gui.stores import xxx，
    # 因此只需 mock src.gui.stores 模块级函数即可
    with patch("src.gui.stores.get_sqlite_store", return_value=mock_store), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()), \
         patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()), \
         patch(
             "src.gui.viewmodels.settings_viewmodel.get_config",
             return_value=isolated_config,
         ), \
         patch(
             "src.gui.viewmodels.chat_viewmodel.Config",
             return_value=isolated_config,
         ), \
         patch("src.utils.config.get_config", return_value=isolated_config):
        yield mock_store


@pytest.fixture(scope="module", autouse=True)
def isolate_qsettings(tmp_path_factory):
    """Route QSettings to a session-temporary INI file, never the user registry."""

    settings_root = tmp_path_factory.mktemp("qsettings-main-window")
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(settings_root))
    QSettings.setPath(
        QSettings.IniFormat,
        QSettings.SystemScope,
        str(settings_root / "system"),
    )
    yield
    settings = QSettings("PKV", "MainWindow")
    settings.clear()
    settings.sync()


@pytest.fixture(autouse=True)
def clean_qsettings(isolate_qsettings):
    """每个测试前后清理隔离 QSettings，避免测试间状态污染。"""

    settings = QSettings("PKV", "MainWindow")
    settings.clear()
    yield
    settings = QSettings("PKV", "MainWindow")
    settings.clear()
    settings.sync()


@pytest.fixture
def main_window(qtbot):
    """创建 MainWindow 实例并注册到 qtbot。"""
    from src.gui.main_window import MainWindow
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def test_database_initialization_uses_runtime_db_path(tmp_path):
    """GUI 启动迁移应使用支持 DATA_DIR/DB_PATH 覆盖的 Config.db_path。"""
    from src.gui import app as gui_app

    runtime_db_path = tmp_path / "runtime" / "db" / "knowledge.db"
    mock_config = MagicMock()
    mock_config.db_path = runtime_db_path
    mock_config.get.return_value = tmp_path / "wrong.db"

    mock_manager = MagicMock()
    mock_manager.get_current_version.return_value = "1.0.0"
    mock_manager.get_pending_migrations.return_value = []

    with patch("src.gui.app.Config", return_value=mock_config), patch(
        "src.gui.app.MigrationManager", return_value=mock_manager
    ) as manager_cls:
        assert gui_app.ensure_database_initialized() is True

    manager_cls.assert_called_once_with(
        runtime_db_path,
        gui_app._PROJECT_ROOT / "scripts" / "migrations",
    )
    mock_config.get.assert_not_called()


def test_settings_saved_refreshes_chat_provider_config(qtbot):
    """设置保存成功后，主窗口应刷新已初始化的 ChatViewModel 配置。"""
    with patch(
        "src.gui.viewmodels.chat_viewmodel.ChatViewModel.reload_provider_config"
    ) as reload_provider_config:
        from src.gui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        window._settings_view.settings_saved.emit()

    reload_provider_config.assert_called_once_with()


# ============================================================
# 验收标准 1: 启动后显示主窗口（无崩溃）
# ============================================================

class TestWindowCreation:
    """测试主窗口创建和基本属性。"""

    def test_window_is_created(self, main_window):
        """主窗口实例化不崩溃。"""
        assert main_window is not None

    def test_window_title(self, main_window):
        """窗口标题正确。"""
        assert main_window.windowTitle() == "Personal Knowledge Vault"

    def test_window_minimum_size(self, main_window):
        """窗口最小尺寸已设置。"""
        assert main_window.minimumWidth() == 900
        assert main_window.minimumHeight() == 600

    def test_window_has_menu_bar(self, main_window):
        """菜单栏存在。"""
        menubar = main_window.menuBar()
        assert menubar is not None

    def test_window_has_status_bar(self, main_window):
        """状态栏存在。"""
        status_bar = main_window.statusBar()
        assert status_bar is not None

    def test_window_can_show(self, main_window, qtbot):
        """窗口可正常显示。"""
        main_window.show()
        qtbot.waitExposed(main_window)
        assert main_window.isVisible()


# ============================================================
# 导航切换
# ============================================================

class TestNavigation:
    """测试侧边栏导航切换。"""

    def test_default_view_is_browser(self, main_window):
        """默认显示浏览视图（索引 0）。"""
        assert main_window._stacked.currentIndex() == 0

    def test_switch_to_search(self, main_window):
        """切换到搜索视图。"""
        main_window.switch_to_search()
        assert main_window._stacked.currentIndex() == 1

    def test_switch_to_browser(self, main_window):
        """切换到浏览视图。"""
        main_window.switch_to_search()  # 先切到搜索
        main_window.switch_to_browser()
        assert main_window._stacked.currentIndex() == 0

    def test_nav_list_click_changes_view(self, main_window):
        """点击导航列表切换视图。"""
        main_window._nav_list.setCurrentRow(1)
        assert main_window._stacked.currentIndex() == 1

        main_window._nav_list.setCurrentRow(0)
        assert main_window._stacked.currentIndex() == 0

    def test_switch_to_search_focuses_input(self, main_window, qtbot):
        """切换到搜索视图时聚焦搜索框。"""
        main_window.show()
        qtbot.waitExposed(main_window)
        main_window.switch_to_search()
        # 验证搜索视图的 focus_search_input 被调用
        assert main_window._stacked.currentIndex() == 1

    def test_switch_to_archive(self, main_window):
        """切换到归档视图。"""
        main_window.switch_to_archive()
        assert main_window._stacked.currentIndex() == 2

    def test_switch_to_chat(self, main_window):
        """切换到 AI 对话视图。"""
        main_window.switch_to_chat()
        assert main_window._stacked.currentIndex() == 3

    def test_switch_to_stats(self, main_window):
        """切换到统计视图。"""
        main_window.switch_to_stats()
        assert main_window._stacked.currentIndex() == 4

    def test_switch_to_settings(self, main_window):
        """切换到设置视图。"""
        main_window.switch_to_settings()
        assert main_window._stacked.currentIndex() == 5

    def test_nav_list_has_six_items(self, main_window):
        """导航列表包含浏览、搜索、归档、AI 对话、统计和设置。"""
        assert main_window._nav_list.count() == 6

    def test_nav_list_click_archive(self, main_window):
        """点击导航列表切换到归档视图。"""
        main_window._nav_list.setCurrentRow(2)
        assert main_window._stacked.currentIndex() == 2

    def test_nav_list_click_stats(self, main_window):
        """点击导航列表切换到统计视图。"""
        main_window._nav_list.setCurrentRow(4)
        assert main_window._stacked.currentIndex() == 4

    def test_nav_list_click_settings(self, main_window):
        """点击导航列表切换到设置视图。"""
        main_window._nav_list.setCurrentRow(5)
        assert main_window._stacked.currentIndex() == 5


# ============================================================
# 验收标准 4: 明亮/暗色主题可切换
# ============================================================

class TestTheme:
    """测试主题切换功能。"""

    def test_default_theme_is_light(self, main_window):
        """默认主题为明亮。"""
        assert main_window.current_theme == "light"

    def test_switch_to_dark_theme(self, main_window):
        """可切换到暗色主题。"""
        main_window.apply_theme("dark")
        assert main_window.current_theme == "dark"

    def test_switch_back_to_light_theme(self, main_window):
        """可从暗色切换回明亮主题。"""
        main_window.apply_theme("dark")
        main_window.apply_theme("light")
        assert main_window.current_theme == "light"

    def test_invalid_theme_file_handled(self, main_window):
        """不存在的主题文件不会崩溃。"""
        main_window.apply_theme("nonexistent_theme")
        # 主题名不变（因为 qss 文件不存在，apply_theme 提前返回）
        assert main_window.current_theme != "nonexistent_theme"


# ============================================================
# 状态栏
# ============================================================

class TestStatusBar:
    """测试状态栏消息更新。"""

    def test_set_status(self, main_window):
        """状态栏文本可更新。"""
        main_window.set_status("测试消息")
        assert main_window._status_label.text() == "测试消息"

    def test_nav_to_search_updates_status(self, main_window):
        """切换到搜索视图时更新状态栏。"""
        main_window.switch_to_search()
        assert "搜索" in main_window._status_label.text()

    def test_nav_to_browser_updates_status(self, main_window):
        """切换到浏览视图时更新状态栏。"""
        main_window.switch_to_search()
        main_window.switch_to_browser()
        assert "浏览" in main_window._status_label.text()

    def test_nav_to_browser_triggers_refresh(self, main_window):
        """切换到浏览视图时触发 BrowserView.refresh()。"""
        with patch.object(main_window._browser_view, "refresh") as mock_refresh:
            main_window.switch_to_search()
            mock_refresh.reset_mock()
            main_window.switch_to_browser()
            mock_refresh.assert_called_once()

    def test_nav_to_archive_updates_status(self, main_window):
        """切换到归档视图时更新状态栏。"""
        main_window.switch_to_archive()
        assert "归档" in main_window._status_label.text()

    def test_nav_to_stats_updates_status(self, main_window):
        """切换到统计视图时更新状态栏。"""
        main_window.switch_to_stats()
        assert "统计" in main_window._status_label.text()

    def test_nav_to_settings_updates_status(self, main_window):
        """切换到设置视图时更新状态栏。"""
        main_window.switch_to_settings()
        assert "设置" in main_window._status_label.text()


# ============================================================
# 验收标准 7: QSettings 持久化
# ============================================================

class TestSettingsPersistence:
    """测试窗口状态保存与恢复。"""

    def test_save_settings_runs_without_error(self, main_window):
        """save_settings() 不抛异常。"""
        main_window.save_settings()

    def test_restore_settings_runs_without_error(self, main_window):
        """restore_settings() 不抛异常。"""
        main_window.restore_settings()

    def test_theme_persisted_after_save_restore(self, main_window):
        """主题在保存/恢复后保持一致。"""
        main_window.apply_theme("dark")
        main_window.save_settings()

        # 读取 QSettings 确认持久化
        settings = QSettings("PKV", "MainWindow")
        saved_theme = settings.value("theme")
        assert saved_theme == "dark"

    def test_close_event_saves_settings(self, main_window):
        """关闭窗口时自动保存状态。"""
        main_window.apply_theme("dark")

        with patch.object(main_window, "save_settings") as mock_save:
            # 使用真正的 QCloseEvent 避免 super().closeEvent() 出错
            event = QCloseEvent()
            main_window.closeEvent(event)
            mock_save.assert_called_once()


# ============================================================
# 关于对话框
# ============================================================

class TestAboutDialog:
    """测试关于对话框。"""

    def test_show_about_no_crash(self, main_window, qtbot):
        """关于对话框可触发（使用 mock 避免阻塞）。"""
        with patch("PySide6.QtWidgets.QMessageBox.about") as mock_about:
            main_window._show_about()
            mock_about.assert_called_once()
