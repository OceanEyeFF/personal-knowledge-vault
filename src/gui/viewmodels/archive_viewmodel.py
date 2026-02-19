"""归档操作 ViewModel。

提供 ArchiveWorker（QThread 后台线程）和 ArchiveViewModel（状态管理），
支持 URL 归档和纯文本归档两种模式。

归档流程与 src/mcp/tools.py 的 archive_url / archive_text 保持一致：
- URL 模式: validate_url_security → WorkflowEngine.execute_async("archive-url")
- 文本模式: validate_text_length → TextFallbackProcessor.process() → WorkflowEngine.execute_async("archive-text")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Literal, Optional

from PySide6.QtCore import QObject, QThread, Signal

from src.mcp.utils import validate_url_security, validate_text_length

logger = logging.getLogger("pkv.gui.viewmodels.archive")


# ============================================================
# ArchiveWorker — 后台归档线程
# ============================================================


class ArchiveWorker(QThread):
    """后台归档工作线程。

    在独立线程中运行 asyncio 事件循环，执行归档工作流，
    避免阻塞 GUI 主线程。

    Signals:
        progress_text: 进度文本消息。
        finished_ok: 归档成功，携带结果数据字典。
        finished_err: 归档失败，携带错误消息字符串。
    """

    progress_text = Signal(str)
    finished_ok = Signal(dict)
    finished_err = Signal(str)

    def __init__(
        self,
        mode: Literal["url", "text"],
        data: Dict[str, Any],
        parent: Optional[QObject] = None,
    ) -> None:
        """初始化归档工作线程。

        Args:
            mode: 归档模式，"url" 或 "text"。
            data: 归档数据字典。
                - URL 模式: {"url": "https://..."}
                - 文本模式: {"text": "...", "title": "..."}
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._mode = mode
        self._data = data

    def run(self) -> None:
        """线程入口：创建独立事件循环并执行异步归档。"""
        try:
            asyncio.run(self._execute())
        except Exception as exc:
            logger.error(f"ArchiveWorker 异常: {exc}")
            self.finished_err.emit(f"归档异常: {exc}")

    async def _execute(self) -> None:
        """异步执行归档工作流。

        根据 mode 分发到对应的归档逻辑：
        - "url": URL 安全验证 → archive-url 工作流
        - "text": 文本长度验证 → TextFallbackProcessor → archive-text 工作流
        """
        if self._mode == "url":
            await self._execute_url()
        elif self._mode == "text":
            await self._execute_text()
        else:
            self.finished_err.emit(f"未知归档模式: {self._mode}")

    async def _execute_url(self) -> None:
        """执行 URL 归档（与 src/mcp/tools.py archive_url 一致）。"""
        url = self._data.get("url", "").strip()
        self.progress_text.emit("正在验证 URL...")

        # 前置安全验证
        valid, error = validate_url_security(url)
        if not valid:
            self.finished_err.emit(error)
            return

        try:
            self.progress_text.emit("正在抓取网页内容...")
            from src.workflow.engine import WorkflowEngine

            engine = WorkflowEngine()
            self.progress_text.emit("正在执行归档工作流（AI 分析中）...")
            result = await engine.execute_async("archive-url", {"url": url})

            if result.success:
                logger.info(
                    f"URL 归档成功: kid={result.data.get('knowledge_id', '')}, "
                    f"title={result.data.get('title', '')!r}"
                )
                self.progress_text.emit("归档完成!")
                self.finished_ok.emit(result.data)
            else:
                error_msg = result.errors[0] if result.errors else "归档失败"
                logger.warning(f"URL 归档失败: url={url!r}, errors={result.errors}")
                self.finished_err.emit(error_msg)
        except Exception as exc:
            logger.error(f"URL 归档异常: {exc}")
            self.finished_err.emit(f"归档异常: {exc}")

    async def _execute_text(self) -> None:
        """执行纯文本归档（与 src/mcp/tools.py archive_text 一致）。"""
        text = self._data.get("text", "")
        title = self._data.get("title", "").strip()
        self.progress_text.emit("正在验证文本...")

        # 前置安全验证：文本长度
        valid, error = validate_text_length(text)
        if not valid:
            self.finished_err.emit(error)
            return

        try:
            # 步骤 1: 用 TextFallbackProcessor 解析文本，获得 Entry 对象
            self.progress_text.emit("正在解析文本内容...")
            from src.processors.text_fallback_processor import TextFallbackProcessor

            processor = TextFallbackProcessor()
            entry = await processor.process(text)

            # 如果提供了 title，覆盖自动提取的标题
            if title and title.strip():
                entry.title = title.strip()

            # 步骤 2: 将 Entry 注入工作流上下文，执行 ai_analyze → store_entry
            self.progress_text.emit("正在执行归档工作流（AI 分析中）...")
            from src.workflow.engine import WorkflowEngine

            engine = WorkflowEngine()
            result = await engine.execute_async(
                "archive-text",
                {
                    "text": text,
                    "title": entry.title,
                    "entry": entry,
                    "content": entry.content,
                },
            )

            if result.success:
                logger.info(
                    f"文本归档成功: kid={result.data.get('knowledge_id', '')}, "
                    f"title={result.data.get('title', entry.title)!r}"
                )
                self.progress_text.emit("归档完成!")
                self.finished_ok.emit(result.data)
            else:
                error_msg = result.errors[0] if result.errors else "归档失败"
                logger.warning(f"文本归档失败: errors={result.errors}")
                self.finished_err.emit(error_msg)
        except Exception as exc:
            logger.error(f"文本归档异常: {exc}")
            self.finished_err.emit(f"归档异常: {exc}")


# ============================================================
# ArchiveViewModel — 归档状态管理
# ============================================================


class ArchiveViewModel(QObject):
    """归档操作的 ViewModel，管理 ArchiveWorker 生命周期。

    提供统一的归档接口（URL / 纯文本），通过信号通知视图层
    状态变化、进度更新、结果数据和错误信息。

    状态机:
        idle → running → success | error → idle（下次归档时重置）

    Signals:
        state_changed: 状态变更通知（"idle" / "running" / "success" / "error"）。
        progress_text: 进度文本消息。
        result_ready: 归档成功，携带结果数据字典。
        error_occurred: 归档失败，携带错误消息字符串。
    """

    state_changed = Signal(str)
    progress_text = Signal(str)
    result_ready = Signal(dict)
    error_occurred = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """初始化 ArchiveViewModel。

        Args:
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self._worker: Optional[ArchiveWorker] = None

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def archive_url(self, url: str) -> None:
        """归档 URL。

        验证 URL 非空后启动后台工作线程。

        Args:
            url: 要归档的网页链接。
        """
        url = url.strip() if url else ""
        if not url:
            self.error_occurred.emit("URL 不能为空")
            self.state_changed.emit("error")
            return

        self._start_worker("url", {"url": url})

    def archive_text(self, text: str, title: str = "") -> None:
        """归档纯文本。

        验证文本非空后启动后台工作线程。

        Args:
            text: 要归档的文本内容。
            title: 可选标题（不提供则自动提取）。
        """
        if not text or not text.strip():
            self.error_occurred.emit("文本内容不能为空")
            self.state_changed.emit("error")
            return

        self._start_worker("text", {"text": text, "title": title})

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _start_worker(self, mode: Literal["url", "text"], data: Dict[str, Any]) -> None:
        """创建并启动归档工作线程。

        如果已有工作线程在运行，则忽略新请求。

        注意：isRunning() 检查和线程启动之间存在理论上的竞态窗口，
        但由于本方法只在 Qt 主线程中调用（通过 UI 交互触发），
        不会有并发调用，因此是安全的。

        Args:
            mode: 归档模式（"url" 或 "text"）。
            data: 归档数据字典。
        """
        if self._worker is not None and self._worker.isRunning():
            logger.warning("归档工作线程正在运行，忽略新请求")
            return

        self.state_changed.emit("running")

        self._worker = ArchiveWorker(mode, data, parent=self)
        self._worker.progress_text.connect(self.progress_text)
        self._worker.finished_ok.connect(self._on_worker_ok)
        self._worker.finished_err.connect(self._on_worker_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

        logger.info(f"归档工作线程已启动: mode={mode}")

    def _on_worker_ok(self, data: Dict[str, Any]) -> None:
        """处理归档成功。

        Args:
            data: 工作流返回的结果数据。
        """
        self.result_ready.emit(data)
        self.state_changed.emit("success")

    def _on_worker_err(self, msg: str) -> None:
        """处理归档失败。

        Args:
            msg: 错误消息。
        """
        self.error_occurred.emit(msg)
        self.state_changed.emit("error")

    def _on_worker_finished(self) -> None:
        """工作线程结束后清理引用和资源。"""
        if self._worker is not None:
            self._worker.wait()        # 等待线程完全退出
            self._worker.deleteLater() # 调度 Qt 对象销毁
        self._worker = None
