"""
日志工具

统一的日志配置和获取接口
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional, TextIO


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
        emit_guard: Optional[Callable[[], bool]] = None,
        label: str = "日志文件",
        **kwargs,
    ):
        self._pkv_path_validator = path_validator
        self._pkv_emit_guard = emit_guard
        self._pkv_label = label
        if path_validator is not None:
            path_validator(Path(filename), label=label)
        super().__init__(filename, **kwargs)

    def _pkv_verify_open(self) -> None:
        from src.runtime.layout import verify_fd_matches_path

        stream = getattr(self, "stream", None)
        if stream is None:
            return
        verify_fd_matches_path(
            stream.fileno(),
            Path(self.baseFilename),
            label=self._pkv_label,
        )

    def _open(self):
        from src.runtime.layout import open_user_file_nofollow

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

    def emit(self, record: logging.LogRecord) -> None:
        """Persist only records owned by an already-active data mutation lease.

        Console handlers still receive every record.  This guard is used only
        for the product ``pkv.log`` handler so read paths never create, rotate
        or append a data-root file merely by logging a query.
        """

        if self._pkv_emit_guard is not None and not self._pkv_emit_guard():
            return
        super().emit(record)


class LoggerSetup:
    """日志配置管理器"""

    _initialized = False
    _DEFAULT_LOG_FORMAT = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
    _DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    @staticmethod
    def _close_and_remove(root_logger: logging.Logger, handler: logging.Handler) -> None:
        """Remove and close a handler before replacing process logging state."""

        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            # Logging replacement must not make a valid adapter startup fail
            # solely because an already-detached third-party handler is broken.
            pass

    @classmethod
    def _replace_root_handlers(cls, root_logger: logging.Logger) -> None:
        for handler in list(root_logger.handlers):
            cls._close_and_remove(root_logger, handler)

    @classmethod
    def _add_runtime_file_handler(
        cls,
        binding,
        *,
        level: Optional[str | int] = None,
        log_format: Optional[str] = None,
        date_format: Optional[str] = None,
    ) -> bool:
        """Install one delayed, lease- and snapshot-bound ``pkv.log`` handler.

        A reload may temporarily retain an older handler only while an already
        active mutation owns its binding.  Each handler filters exact bindings,
        so old work never writes through a newly reloaded snapshot.  Once the
        old scope drains, it is closed and removed deterministically.
        """

        from src.runtime.file_logging import (
            RuntimeFileLogBinding,
            runtime_file_log_binding_is_active,
            runtime_file_log_emit_allowed,
        )

        if not isinstance(binding, RuntimeFileLogBinding):
            raise TypeError("runtime_file_binding must be RuntimeFileLogBinding")
        log_file = binding.path
        root_logger = logging.getLogger()

        for handler in list(root_logger.handlers):
            existing = getattr(handler, "_pkv_runtime_file_binding", None)
            if existing == binding:
                return False
            if existing is not None:
                if runtime_file_log_binding_is_active(existing):
                    setattr(handler, "_pkv_runtime_file_retired", True)
                else:
                    cls._close_and_remove(root_logger, handler)

        file_handler = _ValidatedRotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            # Delayed open is mandatory: merely configuring a READY/read-only
            # adapter must not create pkv.log or its rotation sidecars.
            delay=True,
            path_validator=binding.layout.writable_user_path,
            emit_guard=lambda: runtime_file_log_emit_allowed(binding),
            label="日志文件",
        )
        setattr(file_handler, "_pkv_runtime_file_binding", binding)
        setattr(file_handler, "_pkv_runtime_file_retired", False)
        file_handler.setLevel(level if level is not None else logging.INFO)
        file_handler.setFormatter(
            logging.Formatter(
                log_format or cls._DEFAULT_LOG_FORMAT,
                datefmt=date_format or cls._DEFAULT_DATE_FORMAT,
            )
        )
        root_logger.addHandler(file_handler)
        return True

    @classmethod
    def retire_inactive_runtime_file_handlers(cls) -> None:
        """Close only reload-retired runtime handlers after their scopes drain."""

        from src.runtime.file_logging import runtime_file_log_binding_is_active

        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            binding = getattr(handler, "_pkv_runtime_file_binding", None)
            if binding is None or not getattr(handler, "_pkv_runtime_file_retired", False):
                continue
            if not runtime_file_log_binding_is_active(binding):
                cls._close_and_remove(root_logger, handler)

    @classmethod
    def rebind_runtime_file_handler(cls, binding) -> bool:
        """Replace an already-configured runtime handler for an explicit reload.

        This is intentionally a no-op when the process never opted into file
        logging.  A fresh binding is installed only after a config reload; any
        in-flight old binding is retained until its scoped mutation drains.
        """

        root_logger = logging.getLogger()
        if not any(
            getattr(handler, "_pkv_runtime_file_binding", None) is not None
            for handler in root_logger.handlers
        ):
            return False
        return cls._add_runtime_file_handler(binding, level=root_logger.level)

    @classmethod
    def setup(
        cls,
        level: str = "INFO",
        log_file: Optional[Path] = None,
        log_format: Optional[str] = None,
        date_format: Optional[str] = None,
        *,
        path_validator: Optional[Callable[..., Path]] = None,
        console_stream: Optional[TextIO] = None,
        delay: bool = False,
        create_parent: bool = True,
        emit_guard: Optional[Callable[[], bool]] = None,
        runtime_file_binding=None,
    ):
        """
        设置全局日志配置

        Args:
            level: 日志级别 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            log_file: 日志文件路径（写入前必须通过统一可写叶子合同）
            log_format: 日志格式
            date_format: 时间格式
            path_validator: 已弃用；runtime file logging 从 binding 获取 layout 合同。
            console_stream: 控制台日志目标；默认保持 stdout 兼容
            delay/create_parent/emit_guard: 仅保留参数兼容；data-root file
                logging 一律由 binding 强制 delayed + lease guard。
            runtime_file_binding: 明确 RuntimeLayout + immutable snapshot
                binding；未提供时不得配置文件 handler。
        """
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

        # A reload/reconfiguration closes handlers instead of merely clearing
        # the list.  This releases Windows file handles and cannot leave a
        # closed-over old RuntimeLayout writing after its owner was replaced.
        cls._replace_root_handlers(root_logger)

        # 添加控制台 handler
        console_handler = logging.StreamHandler(
            sys.stdout if console_stream is None else console_stream
        )
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

        # 添加文件 handler (如果指定)
        if log_file is not None:
            if runtime_file_binding is None:
                raise ValueError(
                    "data-root file logging requires an explicit runtime_file_binding"
                )
            if Path(log_file) != runtime_file_binding.path:
                raise ValueError("log_file must match runtime_file_binding.path")
            cls._add_runtime_file_handler(
                runtime_file_binding,
                level=log_level,
                log_format=log_format,
                date_format=date_format,
            )

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
        emit_guard: Optional[Callable[[], bool]] = None,
        runtime_file_binding=None,
    ) -> bool:
        """向根 logger 追加一个受 RuntimeLayout binding 约束的滚动 handler。

        Returns:
            True 表示已添加；False 表示同一路径的 handler 已存在。
        """
        if runtime_file_binding is None:
            raise ValueError(
                "data-root file logging requires an explicit runtime_file_binding"
            )
        if Path(log_file) != runtime_file_binding.path:
            raise ValueError("log_file must match runtime_file_binding.path")
        return cls._add_runtime_file_handler(
            runtime_file_binding,
            level=level,
            log_format=log_format,
            date_format=date_format,
        )


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
