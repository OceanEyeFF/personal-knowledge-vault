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
import logging
import sys
import traceback
from pathlib import Path

# 确保项目根目录在 sys.path 中（支持直接运行该文件的情形）
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QMessageBox

from src.gui.main_window import MainWindow
from src.storage.migration_manager import MigrationManager
from src.utils.config import Config

logger = logging.getLogger("pkv.gui.app")


def ensure_database_initialized() -> bool:
    """确保数据库已初始化（首次运行时自动迁移）。

    检查数据库版本，如果为 "0.0.0"（未初始化），则自动执行所有迁移脚本。

    Returns:
        True: 数据库已就绪，False: 初始化失败
    """
    try:
        # 加载配置
        config = Config()
        db_path = Path(config.get("storage.db_path"))
        migrations_dir = _PROJECT_ROOT / "scripts" / "migrations"

        # 创建迁移管理器
        manager = MigrationManager(db_path, migrations_dir)

        # 检查当前版本
        current_version = manager.get_current_version()

        if current_version == "0.0.0":
            logger.info("检测到首次运行，正在初始化数据库...")
            logger.info(f"数据库路径: {db_path}")
            logger.info(f"迁移脚本目录: {migrations_dir}")

            # 获取待执行的迁移
            pending = manager.get_pending_migrations()
            if not pending:
                logger.error("未找到迁移脚本，无法初始化数据库")
                return False

            logger.info(f"找到 {len(pending)} 个迁移脚本，开始初始化...")

            # 执行所有迁移（首次初始化无需备份）
            success_count = manager.apply_all_pending(auto_backup=False)

            if success_count > 0:
                logger.info(f"✅ 数据库初始化成功！已执行 {success_count} 个迁移脚本")
                logger.info(f"新版本: {manager.get_current_version()}")
                return True
            else:
                logger.error("数据库初始化失败：没有成功执行任何迁移")
                return False
        else:
            # 数据库已存在，检查是否有待升级的迁移
            pending = manager.get_pending_migrations()
            if pending:
                logger.info(f"检测到 {len(pending)} 个待升级的迁移脚本")
                logger.info("提示：请运行 'python scripts/migrate.py' 手动升级数据库")
            else:
                logger.info(f"数据库版本: {current_version} (已是最新)")

            return True

    except Exception as e:
        logger.error(f"数据库初始化检查失败: {e}", exc_info=True)
        return False


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
    app.setApplicationVersion("0.8.0-alpha")
    app.setOrganizationName("PKV")
    # Qt6 默认启用高 DPI 缩放，无需显式设置 AA_EnableHighDpiScaling

    setup_exception_handler(app)

    # 检查并初始化数据库（首次运行时自动迁移）
    logger.info("正在检查数据库状态...")
    if not ensure_database_initialized():
        logger.error("数据库初始化失败，应用无法启动")
        QMessageBox.critical(
            None,
            "数据库初始化失败",
            (
                "数据库初始化失败，应用无法启动。\n\n"
                "可能原因：\n"
                "1. 迁移脚本缺失（请检查 scripts/migrations/ 目录）\n"
                "2. 数据库文件权限不足\n"
                "3. 磁盘空间不足\n\n"
                "请查看日志获取详细错误信息。"
            ),
        )
        return 1

    window = MainWindow()
    window.show()

    logger.info("PKV GUI 已启动 (v0.8.0-alpha)")

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
