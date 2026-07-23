"""应用设置视图。

提供 API 密钥配置、主题选择、检索策略和数据目录信息展示。
设置读写委托给 SettingsViewModel，主题变更通过信号通知 MainWindow。

配置分组:
- API 密钥配置: DeepSeek / OpenAI 的 API Key 和 Base URL
- 显示设置: 界面主题（明亮 / 暗色）
- 检索设置: 默认检索策略
- 数据目录: 只读展示当前配置的数据路径
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.viewmodels.settings_viewmodel import SettingsViewModel

logger = logging.getLogger("pkv.gui.settings")

# ============================================================
# 主题与检索策略选项
# ============================================================

_THEME_OPTIONS: list[tuple[str, str]] = [
    ("明亮", "light"),
    ("暗色", "dark"),
]

_STRATEGY_OPTIONS: list[tuple[str, str]] = [
    ("自动", "auto"),
    ("BM25", "bm25"),
    ("向量", "vector"),
    ("混合", "hybrid"),
]


# ============================================================
# SettingsView 主类
# ============================================================


class SettingsView(QWidget):
    """应用设置视图。

    包含 API 密钥配置、显示设置、检索设置和数据目录信息。
    设置变更通过 SettingsViewModel 持久化到 config/local.yaml。
    主题变更通过 theme_change_requested 信号通知 MainWindow 应用。

    Signals:
        theme_change_requested: 主题变更请求，携带主题名称字符串
            ("light" 或 "dark")，由 MainWindow 调用 apply_theme 处理。
        settings_saved: 本机配置保存成功，供已初始化的组件刷新配置。

    Attributes:
        _vm: 设置 ViewModel 实例。
    """

    theme_change_requested = Signal(str)
    settings_saved = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化设置视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        self._vm = SettingsViewModel(self)
        self._loaded_base_url_values: dict[str, str] = {}

        self._init_ui()
        self._connect_signals()

        # 初始加载设置
        self._load_and_fill()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """构建设置视图布局（多个 QGroupBox 分组）。"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # -- API 密钥配置 --
        api_group = self._build_api_group()
        main_layout.addWidget(api_group)

        # -- 显示设置 --
        display_group = self._build_display_group()
        main_layout.addWidget(display_group)

        # -- 检索设置 --
        retrieval_group = self._build_retrieval_group()
        main_layout.addWidget(retrieval_group)

        # -- 数据目录（只读信息） --
        data_group = self._build_data_group()
        main_layout.addWidget(data_group)

        # -- 底部按钮和状态 --
        bottom_bar = self._build_bottom_bar()
        main_layout.addWidget(bottom_bar)

        # 弹性空间
        main_layout.addStretch()

    def _build_api_group(self) -> QGroupBox:
        """构建 API 密钥配置分组。

        Returns:
            包含 LLM 和 Embedding 配置的 QGroupBox。
        """
        group = QGroupBox("API 密钥配置", self)
        form = QFormLayout(group)
        form.setSpacing(8)

        # LLM API Key
        llm_key_row = QHBoxLayout()
        self._llm_key_input = QLineEdit(self)
        self._llm_key_input.setEchoMode(QLineEdit.Password)  # type: ignore[attr-defined]
        self._llm_key_input.setPlaceholderText("sk-...")
        llm_key_row.addWidget(self._llm_key_input, stretch=1)
        self._llm_key_toggle = QPushButton("显示", self)
        self._llm_key_toggle.setFixedWidth(50)
        llm_key_row.addWidget(self._llm_key_toggle)
        form.addRow("LLM API Key:", llm_key_row)

        # LLM Base URL
        self._llm_url_input = QLineEdit(self)
        self._llm_url_input.setPlaceholderText("https://api.deepseek.com/v1")
        form.addRow("LLM Base URL:", self._llm_url_input)

        # Embedding API Key
        embedding_key_row = QHBoxLayout()
        self._embedding_key_input = QLineEdit(self)
        self._embedding_key_input.setEchoMode(QLineEdit.Password)  # type: ignore[attr-defined]
        self._embedding_key_input.setPlaceholderText("sk-...")
        embedding_key_row.addWidget(self._embedding_key_input, stretch=1)
        self._embedding_key_toggle = QPushButton("显示", self)
        self._embedding_key_toggle.setFixedWidth(50)
        embedding_key_row.addWidget(self._embedding_key_toggle)
        form.addRow("Embedding API Key:", embedding_key_row)

        # Embedding Base URL
        self._embedding_url_input = QLineEdit(self)
        self._embedding_url_input.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Embedding Base URL:", self._embedding_url_input)

        return group

    def _build_display_group(self) -> QGroupBox:
        """构建显示设置分组。

        Returns:
            包含主题选择的 QGroupBox。
        """
        group = QGroupBox("显示设置", self)
        form = QFormLayout(group)
        form.setSpacing(8)

        self._theme_combo = QComboBox(self)
        for display_name, _ in _THEME_OPTIONS:
            self._theme_combo.addItem(display_name)
        self._theme_combo.setFixedWidth(120)
        form.addRow("界面主题:", self._theme_combo)

        return group

    def _build_retrieval_group(self) -> QGroupBox:
        """构建检索设置分组。

        Returns:
            包含检索策略选择的 QGroupBox。
        """
        group = QGroupBox("检索设置", self)
        form = QFormLayout(group)
        form.setSpacing(8)

        self._strategy_combo = QComboBox(self)
        for display_name, _ in _STRATEGY_OPTIONS:
            self._strategy_combo.addItem(display_name)
        self._strategy_combo.setFixedWidth(120)
        form.addRow("默认检索策略:", self._strategy_combo)

        return group

    def _build_data_group(self) -> QGroupBox:
        """构建数据目录信息分组（只读）。

        Returns:
            包含数据路径标签的 QGroupBox。
        """
        group = QGroupBox("数据目录", self)
        form = QFormLayout(group)
        form.setSpacing(6)

        try:
            from src.utils.config import get_config
            config = get_config()
            db_path = str(config.db_path)
            vault_dir = str(config.vault_dir)
            vector_dir = str(config.vector_index_dir)
        except Exception:
            db_path = "(加载失败)"
            vault_dir = "(加载失败)"
            vector_dir = "(加载失败)"

        self._db_path_label = QLabel(db_path, self)
        self._db_path_label.setWordWrap(True)
        self._db_path_label.setProperty("class", "text-muted")
        form.addRow("数据库:", self._db_path_label)

        self._vault_dir_label = QLabel(vault_dir, self)
        self._vault_dir_label.setWordWrap(True)
        self._vault_dir_label.setProperty("class", "text-muted")
        form.addRow("Markdown 目录:", self._vault_dir_label)

        self._vector_dir_label = QLabel(vector_dir, self)
        self._vector_dir_label.setWordWrap(True)
        self._vector_dir_label.setProperty("class", "text-muted")
        form.addRow("向量索引:", self._vector_dir_label)

        return group

    def _build_bottom_bar(self) -> QWidget:
        """构建底部按钮栏（保存 + 重置 + 状态消息）。

        Returns:
            包含操作按钮和状态标签的 QWidget。
        """
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 状态消息标签
        self._status_label = QLabel("", self)
        self._status_label.setProperty("class", "text-muted")
        layout.addWidget(self._status_label, stretch=1)

        # 重置按钮
        self._reset_btn = QPushButton("重置", self)
        self._reset_btn.setFixedWidth(80)
        layout.addWidget(self._reset_btn)

        # 保存按钮
        self._save_btn = QPushButton("保存设置", self)
        self._save_btn.setFixedWidth(100)
        layout.addWidget(self._save_btn)

        return widget

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接控件信号与槽。"""
        # 按钮
        self._save_btn.clicked.connect(self._on_save)
        self._reset_btn.clicked.connect(self._load_and_fill)

        # 密码显示/隐藏切换
        self._llm_key_toggle.clicked.connect(
            lambda: self._toggle_password(self._llm_key_input, self._llm_key_toggle)
        )
        self._embedding_key_toggle.clicked.connect(
            lambda: self._toggle_password(
                self._embedding_key_input, self._embedding_key_toggle
            )
        )

        # ViewModel 信号
        self._vm.settings_saved.connect(self._on_save_success)
        self._vm.error_occurred.connect(self._on_save_error)

    # ------------------------------------------------------------------
    # 设置加载与填充
    # ------------------------------------------------------------------

    def _load_and_fill(self) -> None:
        """从 ViewModel 加载设置并填充所有字段。"""
        settings = self._vm.load_settings()

        # API 密钥
        self._llm_key_input.setText(settings.get("llm_api_key", ""))
        llm_base_url = settings.get("llm_base_url", "")
        self._llm_url_input.setText(llm_base_url)
        self._embedding_key_input.setText(settings.get("embedding_api_key", ""))
        embedding_base_url = settings.get("embedding_base_url", "")
        self._embedding_url_input.setText(embedding_base_url)
        self._loaded_base_url_values = {
            "llm_base_url": llm_base_url,
            "embedding_base_url": embedding_base_url,
        }

        # 主题：从 MainWindow 的 current_theme 读取（ViewModel 返回空字符串占位）
        current_theme = self._get_current_theme()
        theme_index = self._find_combo_index(_THEME_OPTIONS, current_theme)
        self._theme_combo.setCurrentIndex(theme_index)

        # 检索策略
        strategy = settings.get("search_strategy", "auto")
        strategy_index = self._find_combo_index(_STRATEGY_OPTIONS, strategy)
        self._strategy_combo.setCurrentIndex(strategy_index)

        # 清除状态消息
        self._status_label.setText("")

        logger.debug("设置已加载并填充到表单")

    # ------------------------------------------------------------------
    # 保存处理
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        """收集表单数据并保存设置。"""
        # 收集当前表单值
        settings = {
            "llm_api_key": self._llm_key_input.text().strip(),
            "embedding_api_key": self._embedding_key_input.text().strip(),
            "search_strategy": _STRATEGY_OPTIONS[self._strategy_combo.currentIndex()][1],
        }
        current_base_urls = {
            "llm_base_url": self._llm_url_input.text().strip(),
            "embedding_base_url": self._embedding_url_input.text().strip(),
        }
        for key, value in current_base_urls.items():
            if value != self._loaded_base_url_values.get(key):
                settings[key] = value

        # 检查主题是否变更
        new_theme = _THEME_OPTIONS[self._theme_combo.currentIndex()][1]
        current_theme = self._get_current_theme()
        if new_theme != current_theme:
            self.theme_change_requested.emit(new_theme)

        # 委托 ViewModel 保存
        self._vm.save_settings(settings)

    def _on_save_success(self) -> None:
        """保存成功后更新状态消息。"""
        self._loaded_base_url_values = {
            "llm_base_url": self._llm_url_input.text().strip(),
            "embedding_base_url": self._embedding_url_input.text().strip(),
        }
        self._status_label.setText("设置已保存")
        self._status_label.setProperty("status", "success")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        self.settings_saved.emit()
        logger.info("设置保存成功")

    def _on_save_error(self, message: str) -> None:
        """保存失败后显示错误消息。

        Args:
            message: 错误消息字符串。
        """
        self._status_label.setText(f"保存失败: {message}")
        self._status_label.setProperty("status", "error")
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)
        logger.warning(f"设置保存失败: {message}")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _toggle_password(line_edit: QLineEdit, toggle_btn: QPushButton) -> None:
        """切换密码输入框的显示/隐藏模式。

        Args:
            line_edit: 需要切换模式的 QLineEdit。
            toggle_btn: 对应的切换按钮。
        """
        if line_edit.echoMode() == QLineEdit.Password:  # type: ignore[attr-defined]
            line_edit.setEchoMode(QLineEdit.Normal)  # type: ignore[attr-defined]
            toggle_btn.setText("隐藏")
        else:
            line_edit.setEchoMode(QLineEdit.Password)  # type: ignore[attr-defined]
            toggle_btn.setText("显示")

    @staticmethod
    def _find_combo_index(
        options: list[tuple[str, str]],
        value: str,
    ) -> int:
        """根据值查找下拉框选项索引。

        Args:
            options: (显示名, 值) 元组列表。
            value: 要查找的值。

        Returns:
            匹配的索引，未找到时返回 0。
        """
        for i, (_, v) in enumerate(options):
            if v == value:
                return i
        return 0

    def _get_current_theme(self) -> str:
        """获取当前应用主题。

        尝试从 MainWindow 实例读取 current_theme 属性，
        若不可用则返回默认值 "light"。

        Returns:
            当前主题名称（"light" 或 "dark"）。
        """
        # 向上遍历父部件链查找 MainWindow
        widget = self.parent()
        while widget is not None:
            if hasattr(widget, "current_theme"):
                return widget.current_theme  # type: ignore[union-attr]
            widget = widget.parent() if hasattr(widget, "parent") else None
        return "light"
