"""归档操作视图。

提供双标签页归档界面（URL 归档 / 文本归档），
包含进度指示器和结果展示区域。

归档逻辑委托给 ArchiveViewModel，视图层仅负责:
- UI 构建与布局
- 用户输入收集与基本验证
- ViewModel 信号绑定与状态渲染
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.gui.viewmodels.archive_viewmodel import ArchiveViewModel

logger = logging.getLogger("pkv.gui.archive")


# ============================================================
# ArchiveView 主类
# ============================================================


class ArchiveView(QWidget):
    """归档操作视图（URL / 纯文本双标签页）。

    提供两种归档模式的输入界面、进度展示和结果反馈。
    归档流程由 ArchiveViewModel 管理，视图层通过信号驱动
    UI 状态更新。

    Signals:
        navigate_to_browser: 用户点击"前往浏览"时发射，
            通知 MainWindow 切换到浏览视图。

    Attributes:
        _vm: 归档操作 ViewModel 实例。
    """

    navigate_to_browser = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """初始化归档视图。

        Args:
            parent: Qt 父部件。
        """
        super().__init__(parent)
        self._vm = ArchiveViewModel(self)

        self._init_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _init_ui(self) -> None:
        """构建归档视图布局（标签页 + 进度区 + 结果区）。"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # -- 标签页区域 --
        self._tab_widget = self._build_tab_widget()
        main_layout.addWidget(self._tab_widget)

        # -- 进度区域（初始隐藏） --
        self._progress_area = self._build_progress_area()
        self._progress_area.setVisible(False)
        main_layout.addWidget(self._progress_area)

        # -- 结果区域（初始隐藏） --
        self._result_area = self._build_result_area()
        self._result_area.setVisible(False)
        main_layout.addWidget(self._result_area)

        # 弹性空间，将内容顶部对齐
        main_layout.addStretch()

    def _build_tab_widget(self) -> QTabWidget:
        """构建双标签页（URL 归档 / 文本归档）。

        Returns:
            包含两个归档标签页的 QTabWidget。
        """
        tab_widget = QTabWidget(self)

        # Tab 1: URL 归档
        url_tab = self._build_url_tab()
        tab_widget.addTab(url_tab, "URL 归档")

        # Tab 2: 文本归档
        text_tab = self._build_text_tab()
        tab_widget.addTab(text_tab, "文本归档")

        return tab_widget

    def _build_url_tab(self) -> QWidget:
        """构建 URL 归档标签页。

        Returns:
            包含 URL 输入框和归档按钮的 QWidget。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # URL 输入行
        url_row = QHBoxLayout()
        url_row.setSpacing(8)

        self._url_input = QLineEdit(self)
        self._url_input.setPlaceholderText("输入要归档的网页链接...")
        url_row.addWidget(self._url_input, stretch=1)

        self._archive_url_btn = QPushButton("归档", self)
        self._archive_url_btn.setFixedWidth(80)
        url_row.addWidget(self._archive_url_btn)

        layout.addLayout(url_row)
        layout.addStretch()

        return widget

    def _build_text_tab(self) -> QWidget:
        """构建文本归档标签页。

        Returns:
            包含标题输入、文本输入和归档按钮的 QWidget。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题输入
        self._title_input = QLineEdit(self)
        self._title_input.setPlaceholderText("标题（可选）")
        layout.addWidget(self._title_input)

        # 文本内容输入
        self._text_input = QPlainTextEdit(self)
        self._text_input.setPlaceholderText("输入要归档的文本内容...")
        self._text_input.setMinimumHeight(120)
        layout.addWidget(self._text_input)

        # 归档按钮
        self._archive_text_btn = QPushButton("归档", self)
        self._archive_text_btn.setFixedWidth(80)
        layout.addWidget(self._archive_text_btn, alignment=Qt.AlignRight)  # type: ignore[arg-type]

        return widget

    def _build_progress_area(self) -> QWidget:
        """构建进度展示区域（脉冲进度条 + 文本标签）。

        Returns:
            包含进度条和进度标签的 QWidget。
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 脉冲进度条（不确定模式）
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setRange(0, 0)  # 不确定模式，脉冲动画
        self._progress_bar.setTextVisible(False)
        layout.addWidget(self._progress_bar)

        # 进度文本标签
        self._progress_label = QLabel("准备中...", self)
        self._progress_label.setStyleSheet("color: gray; font-size: 12px;")
        layout.addWidget(self._progress_label)

        return widget

    def _build_result_area(self) -> QFrame:
        """构建归档结果展示区域。

        Returns:
            包含结果信息标签和"前往浏览"按钮的 QFrame。
        """
        frame = QFrame(self)
        frame.setFrameShape(QFrame.StyledPanel)  # type: ignore[attr-defined]
        frame.setStyleSheet("QFrame { padding: 8px; }")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # 结果标题
        self._result_title_label = QLabel("", self)
        self._result_title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        self._result_title_label.setWordWrap(True)
        layout.addWidget(self._result_title_label)

        # knowledge_id
        self._result_kid_label = QLabel("", self)
        self._result_kid_label.setStyleSheet("color: gray; font-size: 12px;")
        self._result_kid_label.setWordWrap(True)
        layout.addWidget(self._result_kid_label)

        # 文件路径
        self._result_path_label = QLabel("", self)
        self._result_path_label.setStyleSheet("color: gray; font-size: 12px;")
        self._result_path_label.setWordWrap(True)
        layout.addWidget(self._result_path_label)

        # "前往浏览"按钮
        self._navigate_btn = QPushButton("前往浏览", self)
        self._navigate_btn.setFixedWidth(100)
        layout.addWidget(self._navigate_btn, alignment=Qt.AlignRight)  # type: ignore[arg-type]

        return frame

    # ------------------------------------------------------------------
    # 信号连接
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        """连接控件信号与 ViewModel 信号。"""
        # 按钮 / 回车触发归档
        self._archive_url_btn.clicked.connect(self._on_archive_url)
        self._url_input.returnPressed.connect(self._on_archive_url)
        self._archive_text_btn.clicked.connect(self._on_archive_text)

        # "前往浏览"按钮
        self._navigate_btn.clicked.connect(self.navigate_to_browser)

        # ViewModel 信号
        self._vm.state_changed.connect(self._on_state_changed)
        self._vm.progress_text.connect(self._progress_label.setText)
        self._vm.result_ready.connect(self._on_result_ready)
        self._vm.error_occurred.connect(self._on_error)

    # ------------------------------------------------------------------
    # 归档触发
    # ------------------------------------------------------------------

    def _on_archive_url(self) -> None:
        """触发 URL 归档（验证非空后委托给 ViewModel）。"""
        url = self._url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "输入错误", "请输入要归档的 URL")
            return
        self._vm.archive_url(url)

    def _on_archive_text(self) -> None:
        """触发文本归档（验证非空后委托给 ViewModel）。"""
        text = self._text_input.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "输入错误", "请输入要归档的文本内容")
            return
        title = self._title_input.text().strip()
        self._vm.archive_text(text, title)

    # ------------------------------------------------------------------
    # 状态变化处理
    # ------------------------------------------------------------------

    def _on_state_changed(self, state: str) -> None:
        """响应 ViewModel 状态变更，更新 UI 控件状态。

        Args:
            state: 当前状态（"idle" / "running" / "success" / "error"）。
        """
        if state == "running":
            # 禁用按钮，显示进度
            self._archive_url_btn.setEnabled(False)
            self._archive_text_btn.setEnabled(False)
            self._progress_area.setVisible(True)
            self._result_area.setVisible(False)
        elif state == "success":
            # 启用按钮，隐藏进度（结果区在 _on_result_ready 中显示）
            self._archive_url_btn.setEnabled(True)
            self._archive_text_btn.setEnabled(True)
            self._progress_area.setVisible(False)
        elif state == "error":
            # 启用按钮，隐藏进度（错误信息在 _on_error 中处理）
            self._archive_url_btn.setEnabled(True)
            self._archive_text_btn.setEnabled(True)
            self._progress_area.setVisible(False)
        elif state == "idle":
            # 恢复初始状态
            self._archive_url_btn.setEnabled(True)
            self._archive_text_btn.setEnabled(True)
            self._progress_area.setVisible(False)

    def _on_result_ready(self, data: Dict[str, Any]) -> None:
        """响应归档成功，展示结果信息。

        Args:
            data: 工作流返回的结果数据字典。
        """
        title = data.get("title", "未知标题")
        kid = data.get("knowledge_id", "")
        file_path = data.get("file_path", "")

        self._result_title_label.setText(f"归档成功: {title}")
        self._result_kid_label.setText(f"ID: {kid}")
        self._result_path_label.setText(f"文件: {file_path}")
        self._result_area.setVisible(True)

        logger.info(f"归档结果已展示: kid={kid}, title={title!r}")

    def _on_error(self, message: str) -> None:
        """响应归档错误，显示错误信息。

        Args:
            message: 错误消息字符串。
        """
        self._result_title_label.setText(f"归档失败")
        self._result_kid_label.setText(message)
        self._result_path_label.setText("")
        self._result_area.setVisible(True)
        self._navigate_btn.setVisible(False)

        logger.warning(f"归档失败: {message}")

        QMessageBox.warning(self, "归档失败", message)

        # 恢复导航按钮可见性（为下次成功归档准备）
        self._navigate_btn.setVisible(True)
