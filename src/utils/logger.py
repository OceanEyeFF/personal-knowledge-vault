"""
日志工具

统一的日志配置和获取接口
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from src.runtime.layout import open_user_file_nofollow, verify_fd_matches_path


class _ValidatedRotatingFileHandler(RotatingFileHandler):
    """Rotating file handler bound to the unified writable-leaf contract.

    The target is validated before every open/rollover, and the opened stream
    is verified against ``lstat`` of the path before any record is written.
    Logging cannot receive an open fd, so a leaf swapped for a link between
    validation and open is rejected here (no bytes were written yet); a
    whole-directory swap remains a documented platform limit.
    """

    def __init__(
        self,
        filename,
        *,
        path_validator: Optional[Callable[..., Path]] = None,
        label: str = "日志文件",
        **kwargs,
    ):
        self._pkv_path_validator = path_validator
        self._pkv_label = label
        if path_validator is not None:
            path_validator(Path(filename), label=label)
        super().__init__(filename, **kwargs)

    def _pkv_verify_open(self) -> None:
        stream = getattr(self, "stream", None)
        if stream is None:
            return
        verify_fd_matches_path(
            stream.fileno(),
            Path(self.baseFilename),
            label=self._pkv_label,
        )

    def _open(self):
        if self._pkv_path_validator is not None:
            self._pkv_path_validator(
                Path(self.baseFilename),
                label=self._pkv_label,
            )
        # Bind verification to the stream being returned.  FileHandler assigns
        # ``self.stream`` only after _open() returns, so consulting self.stream
        # here would silently skip the first/delayed open.
        return open_user_file_nofollow(
            Path(self.baseFilename),
            self.mode,
            label=self._pkv_label,
            encoding=self.encoding,
            errors=getattr(self, "errors", None),
        )

    def doRollover(self):
        if self._pkv_path_validator is not None:
            self._pkv_path_validator(
                Path(self.baseFilename),
                label=self._pkv_label,
            )
        super().doRollover()


class LoggerSetup:
    """日志配置管理器"""

    _initialized = False
    _DEFAULT_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
    _DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    @classmethod
    def setup(
        cls,
        level: str = "INFO",
        log_file: Optional[Path] = None,
        log_format: Optional[str] = None,
        date_format: Optional[str] = None,
        *,
        path_validator: Optional[Callable[..., Path]] = None,
    ):
        """
        设置全局日志配置

        Args:
            level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_file: 日志文件路径（写入前必须通过统一可写叶子合同）
            log_format: 日志格式
            date_format: 时间格式
            path_validator: 可写叶子验证器（由 adapter 注入 layout 合同）
        """
        if cls._initialized:
            return

        # 设置日志级别
        log_level = getattr(logging, level.upper(), logging.INFO)

        # 设置日志格式
        if log_format is None:
            log_format = cls._DEFAULT_LOG_FORMAT

        if date_format is None:
            date_format = cls._DEFAULT_DATE_FORMAT

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
            log_file = Path(log_file)
            if path_validator is not None:
                path_validator(log_file, label="日志文件")

            log_file.parent.mkdir(parents=True, exist_ok=True)

            file_handler = _ValidatedRotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
                path_validator=path_validator,
                label="日志文件",
            )
            file_handler.setLevel(log_level)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)

        cls._initialized = True

    @classmethod
    def add_file_handler(
        cls,
        log_file,
        *,
        path_validator: Optional[Callable[..., Path]] = None,
        level: Optional[str] = None,
        log_format: Optional[str] = None,
        date_format: Optional[str] = None,
        delay: bool = False,
    ) -> bool:
        """向根 logger 追加一个已验证的滚动文件 handler（按路径幂等）。

        Returns:
            True 表示已添加；False 表示同一路径的 handler 已存在。
        """
        log_file = Path(log_file)
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if (
                isinstance(handler, _ValidatedRotatingFileHandler)
                and Path(handler.baseFilename) == log_file
            ):
                return False

        if path_validator is not None:
            path_validator(log_file, label="日志文件")

        file_handler = _ValidatedRotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
            delay=delay,
            path_validator=path_validator,
            label="日志文件",
        )
        file_handler.setLevel(level if level is not None else logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(
                log_format or cls._DEFAULT_LOG_FORMAT,
                datefmt=date_format or cls._DEFAULT_DATE_FORMAT,
            )
        )
        root_logger.addHandler(file_handler)
        return True


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
