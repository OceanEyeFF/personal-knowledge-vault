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
import re
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

from src.gui.viewmodels.archive_viewmodel import (
    ArchiveViewModel,
    sanitize_archive_failure,
)

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
        self._progress_label.setProperty("class", "text-muted")
        layout.addWidget(self._progress_label)

        return widget

    def _build_result_area(self) -> QFrame:
        """构建归档结果展示区域。

        Returns:
            包含结果信息标签和"前往浏览"按钮的 QFrame。
        """
        frame = QFrame(self)
        frame.setObjectName("archive_result_frame")

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        # 结果标题
        self._result_title_label = QLabel("", self)
        self._result_title_label.setObjectName("archive_result_title")
        self._result_title_label.setWordWrap(True)
        layout.addWidget(self._result_title_label)

        # knowledge_id
        self._result_kid_label = QLabel("", self)
        self._result_kid_label.setProperty("class", "text-muted")
        self._result_kid_label.setWordWrap(True)
        layout.addWidget(self._result_kid_label)

        # 文件路径
        self._result_path_label = QLabel("", self)
        self._result_path_label.setProperty("class", "text-muted")
        self._result_path_label.setWordWrap(True)
        layout.addWidget(self._result_path_label)

        # 持久可见的降级/修复提示（不能只依赖瞬时对话框）
        self._result_warning_label = QLabel("", self)
        self._result_warning_label.setProperty("class", "text-warning")
        self._result_warning_label.setWordWrap(True)
        self._result_warning_label.setVisible(False)
        layout.addWidget(self._result_warning_label)

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
        self._vm.failure_ready.connect(self._on_error)

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
            state: 当前状态（idle/running/success/degraded/error）。
        """
        if state == "running":
            # 禁用按钮，显示进度
            self._archive_url_btn.setEnabled(False)
            self._archive_text_btn.setEnabled(False)
            self._progress_area.setVisible(True)
            self._result_area.setVisible(False)
        elif state in {"success", "degraded"}:
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
        status = data.get("status")
        terminal = data.get("workflow_terminal", "success")
        workflow_issues = data.get("workflow_issues") or []
        issue_codes = self._issue_codes(workflow_issues)
        storage_degraded = status == "degraded"
        workflow_degraded = terminal == "degraded"

        self._navigate_btn.setVisible(True)
        self._result_warning_label.clear()
        self._result_warning_label.setVisible(False)

        if storage_degraded or workflow_degraded:
            # DEGRADED 仍是核心成功，但辅助索引/可选步骤警告必须持续可见。
            repair_tokens = []
            for raw_action in data.get("repair_actions") or []:
                candidate = str(raw_action)
                repair_tokens.append(
                    candidate
                    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", candidate)
                    else "repair_required"
                )
            repairs = "、".join(repair_tokens)
            warning_parts = ["核心归档已完成，但本次结果处于降级状态。"]
            if storage_degraded:
                warning_parts.append(
                    "辅助索引需要修复"
                    + (f"（修复动作: {repairs}）" if repairs else "（见诊断日志）")
                    + "。"
                )
            if workflow_degraded:
                warning_parts.append(
                    "部分可选工作流步骤未完成"
                    + (f"（问题代码: {issue_codes}）" if issue_codes else "")
                    + "。"
                )
            warning_parts.append("请勿盲目重试归档。")
            warning = "".join(warning_parts)
            self._result_title_label.setText(f"归档成功（降级）: {title}")
            self._result_kid_label.setText(f"ID: {kid}")
            self._result_path_label.setText(f"文件: {file_path}")
            self._result_warning_label.setText(warning)
            self._result_warning_label.setVisible(True)
            self._result_area.setVisible(True)
            logger.warning(
                "归档降级: storage=%s, workflow=%s, issue_codes=%s",
                storage_degraded,
                workflow_degraded,
                issue_codes,
            )
            QMessageBox.warning(self, "归档降级警告", warning)
            return

        self._result_title_label.setText(f"归档成功: {title}")
        self._result_kid_label.setText(f"ID: {kid}")
        self._result_path_label.setText(f"文件: {file_path}")
        self._result_area.setVisible(True)

        safe_knowledge_id = (
            kid if isinstance(kid, int) and not isinstance(kid, bool) else "unknown"
        )
        logger.info(
            "归档结果已展示: knowledge_id=%s",
            safe_knowledge_id,
        )

    def _on_error(self, failure: Dict[str, Any] | str) -> None:
        """响应结构化归档错误，只显示脱敏字段。

        Args:
            failure: ViewModel 结构化失败；字符串仅用于旧调用方兼容。
        """
        if isinstance(failure, dict):
            failure = sanitize_archive_failure(failure)
            code = str(failure.get("code") or "workflow_step_failed")
            stage = str(failure.get("stage") or "workflow")
            recoverable = bool(failure.get("recoverable"))
            message = str(failure.get("safe_message") or "归档失败")
            operation_id = str(failure.get("operation_id") or "")
            repairs = [str(item) for item in failure.get("repair_actions") or []]
            do_not_retry = bool(failure.get("do_not_retry"))
        else:
            code = "workflow_step_failed"
            stage = "workflow"
            recoverable = False
            message = "归档失败（错误代码：workflow_step_failed，阶段：workflow）"
            operation_id = ""
            repairs = []
            do_not_retry = False

        detail_parts = [f"错误代码: {code}", f"阶段: {stage}"]
        detail_parts.append("问题排除后可重试" if recoverable else "需要先检查或修复")
        if operation_id:
            detail_parts.append(f"operation_id: {operation_id}")
        if repairs:
            detail_parts.append(f"修复动作: {', '.join(repairs)}")

        self._result_title_label.setText("归档失败")
        self._result_kid_label.setText(message)
        self._result_path_label.setText("；".join(detail_parts))
        self._result_warning_label.setText("请勿盲目重试归档。" if do_not_retry else "")
        self._result_warning_label.setVisible(do_not_retry)
        self._result_area.setVisible(True)
        self._navigate_btn.setVisible(False)

        logger.warning("归档失败: code=%s, stage=%s", code, stage)

        dialog_message = message + "\n\n" + "；".join(detail_parts)
        if do_not_retry:
            dialog_message += "\n\n请勿盲目重试归档。"
        QMessageBox.warning(self, "归档失败", dialog_message)

    @staticmethod
    def _issue_codes(issues: list[Any]) -> str:
        """Format unique stable workflow issue codes without raw messages."""

        codes: list[str] = []
        for issue in issues:
            raw_code = issue.get("code") if isinstance(issue, dict) else getattr(issue, "code", None)
            candidate = str(getattr(raw_code, "value", raw_code or ""))
            code = (
                candidate
                if re.fullmatch(r"[a-z0-9_]{1,64}", candidate)
                else "workflow_step_failed"
            )
            if code and code not in codes:
                codes.append(code)
        return ", ".join(codes)
