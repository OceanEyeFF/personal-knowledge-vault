"""PKV GUI 应用入口。

提供 main() 函数用于启动 PySide6 桌面应用程序。
支持以下启动方式：
    python src/gui/app.py
    python -m src.gui
    python -m src.gui.app

高 DPI 支持在 Qt6 中默认启用，无需显式设置。
使用 qasync 集成 asyncio 事件循环，支持 @asyncSlot 异步操作。
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# qasync consults QT_API before it checks which Qt binding is already loaded.
# Pin the published binding before importing PySide6 or MainWindow.
os.environ["QT_API"] = "pyside6"

# 确保项目根目录在 sys.path 中（支持直接运行该文件的情形）
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from src import __version__  # noqa: E402
from src.gui.main_window import MainWindow  # noqa: E402
from src.runtime.bootstrap import (  # noqa: E402
    bootstrap_runtime,
    project_bootstrap_error,
)
from src.runtime.errors import PKVRuntimeError  # noqa: E402
from src.utils.config import Config  # noqa: E402

logger = logging.getLogger("pkv.gui.app")
_last_bootstrap_failure: dict[str, object] | None = None

_PUBLIC_EXCEPTION_TYPES = {
    ArithmeticError: "ArithmeticError",
    AssertionError: "AssertionError",
    ImportError: "ImportError",
    LookupError: "LookupError",
    OSError: "OSError",
    RuntimeError: "RuntimeError",
    TypeError: "TypeError",
    ValueError: "ValueError",
}


def _public_exception_type(exc_type: type) -> str:
    """Return a fixed diagnostic label without trusting a class name."""
    return _PUBLIC_EXCEPTION_TYPES.get(exc_type, "Exception")


def get_bootstrap_failure_projection() -> dict[str, object] | None:
    """Return a copy of the last stable GUI startup failure, if any."""

    if _last_bootstrap_failure is None:
        return None
    return dict(_last_bootstrap_failure)


def ensure_database_initialized() -> bool:
    """通过唯一 runtime bootstrap 建立 fresh-install 数据库。

    Returns:
        True: 数据库已就绪，False: 初始化失败
    """
    global _last_bootstrap_failure
    _last_bootstrap_failure = None
    stage = "runtime_configuration"
    try:
        config = Config()
        stage = "runtime_bootstrap"
        context = bootstrap_runtime(config)
        logger.info(
            "数据库已就绪: %s (%s)",
            context.database.current_version,
            context.database.state.value,
        )
        return True

    except PKVRuntimeError as exc:
        _last_bootstrap_failure = project_bootstrap_error(exc, adapter="gui")
        logger.error(
            "GUI runtime bootstrap rejected: %s",
            json.dumps(
                _last_bootstrap_failure,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return False
    except Exception as exc:
        _last_bootstrap_failure = project_bootstrap_error(
            exc,
            adapter="gui",
            stage=stage,
        )
        logger.error(
            "GUI runtime startup failed: %s",
            json.dumps(
                _last_bootstrap_failure,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        return False


def setup_exception_handler(app: QApplication) -> None:
    """设置全局未处理异常捕获并记录稳定、脱敏的诊断。

    KeyboardInterrupt 仍交由系统默认处理（允许 Ctrl+C 终止）。

    Args:
        app: QApplication 实例（当前未直接使用，预留扩展）。
    """
    def handle_exception(
        exc_type: type,
        exc_value: BaseException,
        exc_tb: object,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.error(
            "未处理的异常: code=gui_uncaught_exception error_type=%s",
            _public_exception_type(exc_type),
        )

    sys.excepthook = handle_exception


def main() -> int:
    """启动 PKV GUI 应用程序。

    配置日志、创建 QApplication 和主窗口，使用 qasync 事件循环
    集成 asyncio，支持 ChatViewModel 中的 @asyncSlot 异步操作。

    Returns:
        应用退出码（0 表示正常退出）。
    """
    # 配置基础日志（控制台输出，INFO 级别）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Personal Knowledge Vault")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("PKV")
    # Qt6 默认启用高 DPI 缩放，无需显式设置 AA_EnableHighDpiScaling

    setup_exception_handler(app)

    # 检查并初始化数据库（首次运行时自动迁移）
    logger.info("正在检查数据库状态...")
    if not ensure_database_initialized():
        logger.error("数据库初始化失败，应用无法启动")
        failure = get_bootstrap_failure_projection()
        error_code = (
            failure["code"]
            if failure is not None
            else "gui_bootstrap_failed"
        )
        QMessageBox.critical(
            None,
            "数据库初始化失败",
            (
                "数据库初始化失败，应用无法启动。\n\n"
                "可能原因：\n"
                "1. 发布资源不完整\n"
                "2. 数据库损坏、版本不受支持或需要升级\n"
                "3. 用户数据目录权限/路径不安全或磁盘空间不足\n\n"
                f"错误代码：{error_code}\n"
                "请查看日志获取详细错误信息。"
            ),
        )
        return 1

    window = MainWindow()
    window.show()

    logger.info("PKV GUI 已启动 (v%s)", __version__)

    # 使用 qasync 集成 asyncio 事件循环，
    # 使 @asyncSlot 装饰的异步方法（send_message, archive_url_and_inject）能正常工作
    try:
        from qasync import QEventLoop

        loop = QEventLoop(app)
        asyncio.set_event_loop(loop)
        with loop:
            loop.run_forever()
        return 0
    except ImportError:
        logger.warning(
            "qasync 未安装，回退到标准 Qt 事件循环（异步功能将不可用）"
        )
        return app.exec()


if __name__ == "__main__":
    sys.exit(main())
