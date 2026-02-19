"""PKV 主窗口。

提供 QMainWindow 实现，包含：
- 左侧导航侧边栏（浏览 / 搜索 / 归档 / 统计 / 设置）
- 中央 QStackedWidget（BrowserView / SearchView / ArchiveView / StatsView / SettingsView）
- 菜单栏（文件 / 视图 / 帮助）
- 状态栏（当前状态信息）
- 全局快捷键（Ctrl+K / Ctrl+B / Ctrl+N / Ctrl+Q）
- QSettings 窗口状态与主题持久化
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from src.gui.views.browser_view import BrowserView
from src.gui.views.search_view import SearchView
from src.gui.views.archive_view import ArchiveView
from src.gui.views.stats_view import StatsView
from src.gui.views.settings_view import SettingsView

logger = logging.getLogger("pkv.gui.mainwindow")

# ============================================================
# 导航项索引常量
# ============================================================

_NAV_BROWSER = 0
_NAV_SEARCH = 1
_NAV_ARCHIVE = 2
_NAV_STATS = 3
_NAV_SETTINGS = 4


class MainWindow(QMainWindow):
    """PKV 应用主窗口。

    包含导航侧边栏、中央内容区域、菜单栏、状态栏和快捷键。
    窗口几何信息、布局状态和主题在关闭时通过 QSettings 持久化，
    下次启动时自动恢复。

    Attributes:
        current_theme: 当前应用的主题名称（"light" 或 "dark"）。
    """

    _SETTINGS_ORG = "PKV"
    _SETTINGS_APP = "MainWindow"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化主窗口。

        Args:
            parent: Qt 父部件，通常为 None。
        """
        super().__init__(parent)
        self.current_theme: str = "light"

        self.setWindowTitle("Personal Knowledge Vault")
        self.setMinimumSize(900, 600)
        self.resize(1200, 750)

        self._init_ui()
        self._create_menu_bar()
        self._create_status_bar()
        self._create_shortcuts()

        # 恢复上次窗口状态（几何 + 主题）
        self.restore_settings()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """构建主窗口中央布局（导航侧边栏 + 内容区域）。"""
        # 创建中央容器
        central = QWidget(self)
        self.setCentralWidget(central)

        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # -- 左侧导航侧边栏 --
        nav_panel = self._build_nav_panel()
        layout.addWidget(nav_panel)

        # -- 中央内容区域（QStackedWidget） --
        self._stacked = QStackedWidget(self)
        self._browser_view = BrowserView(self)
        self._search_view = SearchView(self)
        self._stacked.addWidget(self._browser_view)   # 索引 0: 浏览
        self._stacked.addWidget(self._search_view)    # 索引 1: 搜索

        self._archive_view = ArchiveView(self)
        self._stats_view = StatsView(self)
        self._settings_view = SettingsView(self)
        self._stacked.addWidget(self._archive_view)   # 索引 2: 归档
        self._stacked.addWidget(self._stats_view)     # 索引 3: 统计
        self._stacked.addWidget(self._settings_view)  # 索引 4: 设置

        layout.addWidget(self._stacked, stretch=1)

        # 连接新视图信号
        self._archive_view.navigate_to_browser.connect(self.switch_to_browser)
        self._settings_view.theme_change_requested.connect(self.apply_theme)

        # 默认显示浏览视图
        self._stacked.setCurrentIndex(_NAV_BROWSER)

    def _build_nav_panel(self) -> QWidget:
        """构建左侧导航侧边栏（固定宽度约 120px）。

        Returns:
            包含 QListWidget 导航列表的 QWidget。
        """
        panel = QWidget()
        panel.setFixedWidth(120)
        panel.setObjectName("nav_panel")
        panel.setStyleSheet(
            "#nav_panel { border-right: 1px solid #d0d0d0; }"
        )

        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._nav_list = QListWidget(panel)
        self._nav_list.setFrameShape(QListWidget.NoFrame)  # type: ignore[attr-defined]
        self._nav_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)  # type: ignore[attr-defined]

        # 导航项
        browser_item = QListWidgetItem("浏览")
        browser_item.setTextAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        self._nav_list.addItem(browser_item)

        search_item = QListWidgetItem("搜索")
        search_item.setTextAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        self._nav_list.addItem(search_item)

        archive_item = QListWidgetItem("归档")
        archive_item.setTextAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        self._nav_list.addItem(archive_item)

        stats_item = QListWidgetItem("统计")
        stats_item.setTextAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        self._nav_list.addItem(stats_item)

        settings_item = QListWidgetItem("设置")
        settings_item.setTextAlignment(Qt.AlignCenter)  # type: ignore[attr-defined]
        self._nav_list.addItem(settings_item)

        self._nav_list.setCurrentRow(_NAV_BROWSER)
        self._nav_list.currentRowChanged.connect(self._on_nav_changed)

        layout.addWidget(self._nav_list)

        return panel

    # ------------------------------------------------------------------
    # 菜单栏
    # ------------------------------------------------------------------

    def _create_menu_bar(self) -> None:
        """创建菜单栏（文件 / 视图 / 帮助）。"""
        menubar = self.menuBar()

        # -- 文件菜单 --
        file_menu = menubar.addMenu("文件")

        exit_action = QAction("退出", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.setStatusTip("退出应用程序")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # -- 视图菜单 --
        view_menu = menubar.addMenu("视图")

        browser_action = QAction("浏览知识库", self)
        browser_action.setShortcut(QKeySequence("Ctrl+B"))
        browser_action.setStatusTip("切换到知识库浏览视图")
        browser_action.triggered.connect(self.switch_to_browser)
        view_menu.addAction(browser_action)

        search_action = QAction("搜索知识库", self)
        search_action.setShortcut(QKeySequence("Ctrl+K"))
        search_action.setStatusTip("切换到搜索视图并聚焦搜索框")
        search_action.triggered.connect(self.switch_to_search)
        view_menu.addAction(search_action)

        archive_action = QAction("归档内容", self)
        archive_action.setShortcut(QKeySequence("Ctrl+N"))
        archive_action.setStatusTip("切换到归档视图")
        archive_action.triggered.connect(self.switch_to_archive)
        view_menu.addAction(archive_action)

        stats_action = QAction("统计信息", self)
        stats_action.setStatusTip("切换到统计视图")
        stats_action.triggered.connect(self.switch_to_stats)
        view_menu.addAction(stats_action)

        settings_action = QAction("设置", self)
        settings_action.setStatusTip("切换到设置视图")
        settings_action.triggered.connect(self.switch_to_settings)
        view_menu.addAction(settings_action)

        view_menu.addSeparator()

        # 主题切换子菜单
        theme_menu = view_menu.addMenu("切换主题")

        light_action = QAction("明亮主题", self)
        light_action.setStatusTip("应用明亮主题")
        light_action.triggered.connect(lambda: self.apply_theme("light"))
        theme_menu.addAction(light_action)

        dark_action = QAction("暗色主题", self)
        dark_action.setStatusTip("应用暗色主题")
        dark_action.triggered.connect(lambda: self.apply_theme("dark"))
        theme_menu.addAction(dark_action)

        # -- 帮助菜单 --
        help_menu = menubar.addMenu("帮助")

        about_action = QAction("关于 PKV", self)
        about_action.setStatusTip("关于 Personal Knowledge Vault")
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------
    # 状态栏
    # ------------------------------------------------------------------

    def _create_status_bar(self) -> None:
        """创建状态栏。"""
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)
        self._status_label = QLabel("就绪")
        status_bar.addWidget(self._status_label)

    # ------------------------------------------------------------------
    # 全局快捷键
    # ------------------------------------------------------------------

    def _create_shortcuts(self) -> None:
        """注册全局键盘快捷键。

        Ctrl+K: 切换到搜索视图并聚焦搜索框
        Ctrl+B: 切换到浏览视图
        Ctrl+Q: 退出应用
        """
        # Ctrl+K — 切换到搜索（菜单已注册，此处补充直接快捷键）
        shortcut_search = QShortcut(QKeySequence("Ctrl+K"), self)
        shortcut_search.activated.connect(self.switch_to_search)

        # Ctrl+B — 切换到浏览
        shortcut_browser = QShortcut(QKeySequence("Ctrl+B"), self)
        shortcut_browser.activated.connect(self.switch_to_browser)

        # Ctrl+N — 切换到归档
        shortcut_archive = QShortcut(QKeySequence("Ctrl+N"), self)
        shortcut_archive.activated.connect(self.switch_to_archive)

    # ------------------------------------------------------------------
    # 导航切换
    # ------------------------------------------------------------------

    def _on_nav_changed(self, row: int) -> None:
        """响应导航列表选中变化，切换中央视图。

        Args:
            row: 导航项索引（0=浏览, 1=搜索）。
        """
        self._stacked.setCurrentIndex(row)
        if row == _NAV_BROWSER:
            self.set_status("浏览模式")
            self._browser_view.refresh()
        elif row == _NAV_SEARCH:
            self.set_status("搜索模式")
            self._search_view.focus_search_input()
        elif row == _NAV_ARCHIVE:
            self.set_status("归档模式")
        elif row == _NAV_STATS:
            self.set_status("统计模式")
            self._stats_view.refresh()
        elif row == _NAV_SETTINGS:
            self.set_status("设置")

    def switch_to_browser(self) -> None:
        """切换到浏览视图（供菜单和快捷键调用）。"""
        self._nav_list.setCurrentRow(_NAV_BROWSER)

    def switch_to_search(self) -> None:
        """切换到搜索视图并聚焦搜索框（供菜单和快捷键调用）。"""
        self._nav_list.setCurrentRow(_NAV_SEARCH)
        self._search_view.focus_search_input()

    def switch_to_archive(self) -> None:
        """切换到归档视图（供菜单和快捷键调用）。"""
        self._nav_list.setCurrentRow(_NAV_ARCHIVE)

    def switch_to_stats(self) -> None:
        """切换到统计视图（供菜单和快捷键调用）。"""
        self._nav_list.setCurrentRow(_NAV_STATS)

    def switch_to_settings(self) -> None:
        """切换到设置视图（供菜单和快捷键调用）。"""
        self._nav_list.setCurrentRow(_NAV_SETTINGS)

    # ------------------------------------------------------------------
    # 主题管理
    # ------------------------------------------------------------------

    def apply_theme(self, theme: str) -> None:
        """加载并应用指定主题的 QSS 样式表。

        Args:
            theme: 主题名称，"light" 或 "dark"。
        """
        qss_path = Path(__file__).parent / "styles" / f"{theme}.qss"
        if not qss_path.exists():
            logger.warning(f"主题文件不存在: {qss_path}")
            return

        try:
            with open(qss_path, "r", encoding="utf-8") as f:
                qss = f.read()
            app = QApplication.instance()
            if app:
                app.setStyleSheet(qss)
            self.current_theme = theme
            self.set_status(f"已切换主题: {'明亮' if theme == 'light' else '暗色'}")
            logger.info(f"已应用主题: {theme}")
        except Exception as exc:
            logger.error(f"应用主题失败 ({theme}): {exc}")

    # ------------------------------------------------------------------
    # 状态栏消息
    # ------------------------------------------------------------------

    def set_status(self, message: str) -> None:
        """更新状态栏文本。

        Args:
            message: 状态文本。
        """
        self._status_label.setText(message)

    # ------------------------------------------------------------------
    # QSettings 状态持久化
    # ------------------------------------------------------------------

    def save_settings(self) -> None:
        """保存窗口几何、布局状态和当前主题到 QSettings。"""
        settings = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("state", self.saveState())
        settings.setValue("theme", self.current_theme)
        logger.debug("窗口状态已保存")

    def restore_settings(self) -> None:
        """从 QSettings 恢复窗口几何、布局状态和主题。

        若无历史记录则使用默认设置（明亮主题）。
        """
        settings = QSettings(self._SETTINGS_ORG, self._SETTINGS_APP)

        if settings.contains("geometry"):
            geometry = settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)

        if settings.contains("state"):
            state = settings.value("state")
            if state:
                self.restoreState(state)

        theme = settings.value("theme", "light")
        if theme not in ("light", "dark"):
            theme = "light"
        self.apply_theme(theme)
        logger.debug(f"已恢复窗口状态（主题: {theme}）")

    # ------------------------------------------------------------------
    # 窗口事件
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        """窗口关闭时保存状态。

        Args:
            event: Qt 关闭事件。
        """
        self.save_settings()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # 对话框
    # ------------------------------------------------------------------

    def _show_about(self) -> None:
        """显示关于对话框。"""
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.about(
            self,
            "关于 Personal Knowledge Vault",
            (
                "<b>Personal Knowledge Vault</b><br>"
                "版本: v0.8.0-alpha (Phase 2B M10)<br><br>"
                "AI-First 个人知识管理系统<br>"
                "工作流驱动 · 本地优先 · MCP 开放集成"
            ),
        )
