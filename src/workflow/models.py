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

    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典副本。

        Returns:
            状态字典
        """
        return dict(self._data)


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
    """工作流执行结果。"""

    success: bool
    data: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
