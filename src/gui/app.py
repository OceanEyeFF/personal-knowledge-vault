"""PKV GUI 应用入口。

提供 main() 函数用于启动 PySide6 桌面应用程序。
支持以下启动方式：
    python src/gui/app.py
    python -m src.gui
    python -m src.gui.app

高 DPI 支持在 Qt6 中默认启用，无需显式设置。
"""

import logging
import sys
import traceback
from pathlib import Path

# 确保项目根目录在 sys.path 中（支持直接运行该文件的情形）
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtWidgets import QApplication

from src.gui.main_window import MainWindow

logger = logging.getLogger("pkv.gui.app")


def setup_exception_handler(app: QApplication) -> None:
    """设置全局未处理异常捕获，记录日志并打印堆栈。

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
            "未处理的异常",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = handle_exception


def main() -> int:
    """启动 PKV GUI 应用程序。

    配置日志、创建 QApplication 和主窗口，进入 Qt 事件循环。

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
    app.setApplicationVersion("0.8.0-alpha")
    app.setOrganizationName("PKV")
    # Qt6 默认启用高 DPI 缩放，无需显式设置 AA_EnableHighDpiScaling

    setup_exception_handler(app)

    window = MainWindow()
    window.show()

    logger.info("PKV GUI 已启动 (v0.8.0-alpha)")

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
