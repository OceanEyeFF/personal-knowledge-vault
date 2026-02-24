"""
工作流引擎

负责加载配置、编排步骤并输出执行结果。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Type

from src.utils.config import get_workflow_config
from src.workflow.models import WorkflowContext, WorkflowResult
from src.workflow.steps import (
    BaseStep,
    FetchStep,
    AnalyzeStep,
    IdeaSharpenStep,
    ReviewStep,
    StoreStep,
)

_STEP_REGISTRY: Dict[str, Type[BaseStep]] = {
    "fetch_content": FetchStep,
    "ai_analyze": AnalyzeStep,
    "idea_sharpen": IdeaSharpenStep,
    "review_entry": ReviewStep,
    "store_entry": StoreStep,
}


class WorkflowEngine:
    """工作流引擎核心。"""

    def __init__(self, reload_config: bool = False) -> None:
        """
        初始化工作流引擎。

        Args:
            reload_config: 是否每次执行都重新加载配置
        """
        self._reload_config = reload_config
        self._config_cache: Dict[str, Dict[str, Any]] = {}

    def register_step(self, step_type: str, step_class: Type[BaseStep]) -> None:
        """
        注册自定义步骤类型。

        Args:
            step_type: 步骤类型标识
            step_class: 步骤类
        """
        _STEP_REGISTRY[step_type] = step_class

    async def execute_async(self, workflow_name: str, input_data: Dict[str, Any]) -> WorkflowResult:
        """
        异步执行工作流。

        Args:
            workflow_name: 工作流名称
            input_data: 输入数据

        Returns:
            WorkflowResult
        """
        context = WorkflowContext(input_data)
        errors: List[str] = []

        try:
            workflow_config = self._load_workflow_config(workflow_name)
            steps = self._normalize_steps(workflow_config.get("steps"))
        except Exception as e:
            message = f"工作流配置加载失败: {e}"
            context.log(message)
            return WorkflowResult(success=False, data=context.state.to_dict(), errors=[message], logs=context.logs)

        for step_config in steps:
            step_type = step_config.get("type")
            step_id = step_config.get("id") or step_type or "unknown_step"
            if not step_type:
                error = f"步骤配置缺少 type: {step_config}"
                context.log(error)
                errors.append(error)
                continue

            step_class = _STEP_REGISTRY.get(step_type)
            if step_class is None:
                error = f"未知步骤类型: {step_type}"
                context.log(error)
                errors.append(error)
                continue

            # 修复：传递 config 字段而非整个 step_config
            step_config_data = step_config.get("config", {})
            step = step_class(step_id=step_id, config=step_config_data)
            try:
                result = await step.execute(context)
            except Exception as e:
                error = f"步骤 {step_id} 执行失败: {e}"
                context.log(error)
                errors.append(error)
                continue

            if isinstance(result, dict):
                step_errors = result.pop("errors", None)
                if isinstance(step_errors, list):
                    errors.extend(step_errors)

                for key, value in result.items():
                    context.state.set(key, value)

        success = len(errors) == 0
        return WorkflowResult(
            success=success,
            data=context.state.to_dict(),
            errors=errors,
            logs=context.logs,
        )

    def execute(self, workflow_name: str, input_data: Dict[str, Any]) -> WorkflowResult:
        """
        同步执行工作流（包装异步接口）。

        Args:
            workflow_name: 工作流名称
            input_data: 输入数据

        Returns:
            WorkflowResult
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            raise RuntimeError("当前事件循环正在运行，请使用 execute_async")

        return asyncio.run(self.execute_async(workflow_name, input_data))

    def _load_workflow_config(self, workflow_name: str) -> Dict[str, Any]:
        """
        加载工作流配置（带缓存）。

        Args:
            workflow_name: 工作流名称

        Returns:
            配置字典
        """
        if self._reload_config or workflow_name not in self._config_cache:
            self._config_cache[workflow_name] = get_workflow_config(workflow_name)
        return self._config_cache[workflow_name]

    @staticmethod
    def _normalize_steps(steps: Optional[List[Any]]) -> List[Dict[str, Any]]:
        """
        规范化步骤配置。

        Args:
            steps: 原始 steps 配置

        Returns:
            规范化后的步骤列表
        """
        if not steps:
            return []

        if isinstance(steps, list) and steps and isinstance(steps[0], str):
            step_type_map = {
                "fetch": "fetch_content",
                "analyze": "ai_analyze",
                "sharpen": "idea_sharpen",
                "store": "store_entry",
            }
            return [
                {"id": step_name, "type": step_type_map.get(step_name, step_name)}
                for step_name in steps
            ]

        return [step for step in steps if isinstance(step, dict)]
