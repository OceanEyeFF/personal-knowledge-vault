"""
日志工具

统一的日志配置和获取接口
"""

import logging
import sys
from pathlib import Path
from typing import Optional
from logging.handlers import RotatingFileHandler


class LoggerSetup:
    """日志配置管理器"""

    _initialized = False

    @classmethod
    def setup(
        cls,
        level: str = "INFO",
        log_file: Optional[Path] = None,
        log_format: Optional[str] = None,
        date_format: Optional[str] = None,
    ):
        """
        设置全局日志配置

        Args:
            level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_file: 日志文件路径
            log_format: 日志格式
            date_format: 时间格式
        """
        if cls._initialized:
            return

        # 设置日志级别
        log_level = getattr(logging, level.upper(), logging.INFO)

        # 设置日志格式
        if log_format is None:
            log_format = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"

        if date_format is None:
            date_format = "%Y-%m-%d %H:%M:%S"

        formatter = logging.Formatter(log_format, datefmt=date_format)

        # 获取根日志器
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # 清除已有的 handlers
        root_logger.handlers.clear()

        # 添加控制台 handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # 添加文件 handler (如果指定)
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)

            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)

        cls._initialized = True


def get_logger(name: str) -> logging.Logger:
    """
    获取日志器实例

    Args:
        name: 日志器名称 (通常使用 __name__)

    Returns:
        Logger 实例

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("测试日志")
    """
    return logging.getLogger(name)
