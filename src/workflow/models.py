"""
工作流数据模型

包含状态管理、上下文、结果对象
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger

logger = get_logger(__name__)


class State:
    """工作流状态容器，提供简单的 get/set 接口。"""

    def __init__(self, initial: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化 State。

        Args:
            initial: 初始状态字典
        """
        self._data: Dict[str, Any] = dict(initial or {})

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取状态值。

        Args:
            key: 键名
            default: 默认值

        Returns:
            对应的值或默认值
        """
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """
        设置状态值。

        Args:
            key: 键名
            value: 值
        """
        self._data[key] = value

    def has(self, key: str) -> bool:
        """
        判断是否包含指定键。

        Args:
            key: 键名

        Returns:
            是否存在
        """
        return key in self._data

    def pop(self, key: str, default: Any = None) -> Any:
        """Remove and return a transient state value.

        Workflow capabilities are intentionally one-shot and must not be
        copied into the public ``WorkflowResult.data`` mapping.
        """

        return self._data.pop(key, default)

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典副本。

        Returns:
            状态字典
        """
        return dict(self._data)

    def to_result_dict(self) -> Dict[str, Any]:
        """Return adapter-facing state without transient capability entries."""

        return {
            key: value
            for key, value in self._data.items()
            if type(key) is not str or not key.startswith("_pkv_")
        }


class WorkflowContext:
    """工作流上下文，包含状态与日志。"""

    def __init__(self, initial_state: Optional[Dict[str, Any]] = None) -> None:
        """
        初始化上下文。

        Args:
            initial_state: 初始状态
        """
        self.state = State(initial_state)
        self.logs: List[str] = []

    def log(self, message: str) -> None:
        """
        记录日志。

        Args:
            message: 日志内容
        """
        if not message:
            return
        self.logs.append(message)
        logger.info(message)


@dataclass
class WorkflowResult:
    """工作流执行结果。

    ``errors``/``warnings`` 保留给人类可读 adapter；``issues`` 是稳定的
    机器可读合同。``terminal`` 只能是 success/degraded/error，且必须与
    ``success`` 一致。
    """

    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    issues: List[Dict[str, Any]] = field(default_factory=list)
    terminal: Optional[str] = None

    def __post_init__(self) -> None:
        """推导并校验公开终态，避免 adapter 各自猜测结果语义。"""
        if self.terminal is None:
            if not self.success:
                self.terminal = "error"
            elif self.warnings:
                self.terminal = "degraded"
            else:
                self.terminal = "success"

        if self.terminal not in {"success", "degraded", "error"}:
            raise ValueError(f"未知 Workflow 终态: {self.terminal}")
        if self.success != (self.terminal != "error"):
            raise ValueError(
                "WorkflowResult.success 与 terminal 不一致: "
                f"success={self.success}, terminal={self.terminal}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the stable adapter-facing representation."""
        return {
            "success": self.success,
            "terminal": self.terminal,
            "data": dict(self.data),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "issues": [dict(issue) for issue in self.issues],
            "logs": list(self.logs),
        }
