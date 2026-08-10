"""
工作流引擎

负责加载配置、编排步骤并输出执行结果。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional, Type

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.utils.config import get_workflow_config
from src.utils.logger import get_logger
from src.workflow.config_schema import validate_workflow_config
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

logger = get_logger(__name__)


class WorkflowEngine:
    """工作流引擎核心。"""

    def __init__(
        self,
        reload_config: bool = False,
        step_registry: Optional[Mapping[str, Type[BaseStep]]] = None,
    ) -> None:
        """
        初始化工作流引擎。

        Args:
            reload_config: 是否每次执行都重新加载配置
        """
        self._reload_config = reload_config
        self._config_cache: Dict[str, Dict[str, Any]] = {}
        self._step_registry: Dict[str, Type[BaseStep]] = dict(
            _STEP_REGISTRY if step_registry is None else step_registry
        )

    def register_step(self, step_type: str, step_class: Type[BaseStep]) -> None:
        """
        注册自定义步骤类型。

        Args:
            step_type: 步骤类型标识
            step_class: 步骤类
        """
        self._step_registry[step_type] = step_class

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
        warnings: List[str] = []
        issues: List[Dict[str, Any]] = []

        try:
            workflow_config = self._load_workflow_config(workflow_name)
            steps = validate_workflow_config(
                workflow_name,
                workflow_config,
                self._step_registry,
            )
        except Exception as e:
            logger.error(
                "Workflow configuration load/validation failed: cause_type=%s",
                type(e).__name__,
            )
            message = "工作流配置加载或校验失败"
            context.log(message)
            issues.append(
                self._exception_issue(
                    e,
                    message,
                    severity="error",
                    recoverable=True,
                )
            )
            return WorkflowResult(
                success=False,
                terminal="error",
                data=context.state.to_result_dict(),
                errors=[message],
                warnings=[],
                issues=issues,
                logs=context.logs,
            )

        for step_config in steps:
            step_type = step_config["type"]
            step_id = step_config["id"]
            on_error = step_config["on_error"]
            step_class = self._step_registry[step_type]
            step_config_data = step_config["config"]
            try:
                step = step_class(step_id=step_id, config=step_config_data)
                result = await step.execute(context)
            except Exception as e:
                logger.error(
                    "Workflow step failed: step_id=%s cause_type=%s",
                    step_id,
                    type(e).__name__,
                )
                message = f"步骤 {step_id} 执行失败"
                context.log(message)
                issue = self._exception_issue(
                    e,
                    message,
                    severity="error" if on_error == "fail" else "warning",
                    step_id=step_id,
                    recoverable=on_error == "continue",
                )
                issues.append(issue)
                if on_error == "fail":
                    errors.append(message)
                    break
                warnings.append(message)
                continue

            if not isinstance(result, Mapping):
                message = f"步骤 {step_id} 返回值必须是映射"
                context.log(message)
                issue = self._message_issue(
                    message,
                    severity="error" if on_error == "fail" else "warning",
                    step_id=step_id,
                    recoverable=on_error == "continue",
                )
                issues.append(issue)
                if on_error == "fail":
                    errors.append(message)
                    break
                warnings.append(message)
                continue

            step_result = dict(result)
            step_errors = self._message_list(step_result.pop("errors", None), "errors", step_id)
            step_warnings = self._message_list(
                step_result.pop("warnings", None), "warnings", step_id
            )
            step_issues = step_result.pop("issues", None)

            for key, value in step_result.items():
                context.state.set(key, value)

            normalized_step_issues = self._normalize_step_issues(step_issues, step_id)
            for step_issue in normalized_step_issues:
                message = str(step_issue["message"])
                if step_issue["severity"] == "error" and message not in step_errors:
                    step_errors.append(message)
                elif step_issue["severity"] == "warning" and message not in step_warnings:
                    step_warnings.append(message)

            for message in step_warnings:
                warnings.append(message)
                matching = self._matching_issue(normalized_step_issues, message)
                if matching is not None:
                    matching["severity"] = "warning"
                    issues.append(matching)
                else:
                    issues.append(
                        self._message_issue(
                            message,
                            severity="warning",
                            step_id=step_id,
                            recoverable=True,
                            result=step_result,
                        )
                    )

            if step_errors:
                if on_error == "fail":
                    errors.extend(step_errors)
                    for message in step_errors:
                        matching = self._matching_issue(normalized_step_issues, message)
                        if matching is not None:
                            matching["severity"] = "error"
                            issues.append(matching)
                        else:
                            issues.append(
                                self._message_issue(
                                    message,
                                    severity="error",
                                    step_id=step_id,
                                    recoverable=False,
                                    result=step_result,
                                )
                            )
                    break
                warnings.extend(step_errors)
                for message in step_errors:
                    matching = self._matching_issue(normalized_step_issues, message)
                    if matching is not None:
                        matching["severity"] = "warning"
                        issues.append(matching)
                    else:
                        issues.append(
                            self._message_issue(
                                message,
                                severity="warning",
                                step_id=step_id,
                                recoverable=True,
                                result=step_result,
                            )
                        )

        success = not errors
        terminal = "error" if errors else ("degraded" if warnings else "success")
        return WorkflowResult(
            success=success,
            terminal=terminal,
            data=context.state.to_result_dict(),
            errors=errors,
            warnings=warnings,
            issues=issues,
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
    def _message_list(value: object, field: str, step_id: str) -> List[str]:
        """Normalize a step's human-readable control field."""
        if value is None:
            return []
        if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
            return list(value)
        return [f"步骤 {step_id} 返回了无效 {field} 字段"]

    @staticmethod
    def _exception_issue(
        exc: Exception,
        message: str,
        *,
        severity: str,
        step_id: str | None = None,
        recoverable: bool | None = None,
    ) -> Dict[str, Any]:
        if isinstance(exc, PKVRuntimeError):
            issue = exc.to_dict()
            issue["message"] = message
        else:
            issue = {
                "code": (
                    ErrorCode.WORKFLOW_STEP_FAILED.value
                    if step_id
                    else ErrorCode.WORKFLOW_CONFIG_INVALID.value
                ),
                "message": message,
                "recoverable": bool(recoverable),
                "stage": "workflow_step" if step_id else "workflow_configuration",
            }
        issue["severity"] = severity
        issue["cause_type"] = type(exc).__name__
        if step_id:
            issue["step_id"] = step_id
        elif isinstance(issue.get("stage"), str) and ":" in issue["stage"]:
            issue["step_id"] = issue["stage"].split(":", 1)[1]
        return issue

    @staticmethod
    def _message_issue(
        message: str,
        *,
        severity: str,
        step_id: str,
        recoverable: bool,
        result: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        code = ErrorCode.WORKFLOW_STEP_FAILED.value
        if result:
            storage_errors = result.get("storage_errors")
            if isinstance(storage_errors, list):
                for storage_error in storage_errors:
                    if not isinstance(storage_error, Mapping):
                        continue
                    candidate_message = storage_error.get("message")
                    if candidate_message and str(candidate_message) in message:
                        code = str(storage_error.get("code") or code)
                        recoverable = bool(storage_error.get("recoverable", recoverable))
                        break
        return {
            "code": code,
            "message": message,
            "severity": severity,
            "recoverable": recoverable,
            "stage": "workflow_step",
            "step_id": step_id,
        }

    @staticmethod
    def _normalize_step_issues(value: object, step_id: str) -> List[Dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            return [
                {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": f"步骤 {step_id} 返回了无效 issues 字段",
                    "severity": "error",
                    "recoverable": False,
                    "stage": "workflow_step",
                    "step_id": step_id,
                }
            ]
        normalized: List[Dict[str, Any]] = []
        malformed = False
        for raw_issue in value:
            if not isinstance(raw_issue, Mapping):
                malformed = True
                continue
            if not all(key in raw_issue for key in ("code", "message", "severity", "recoverable")):
                malformed = True
                continue
            issue = dict(raw_issue)
            if not (
                isinstance(issue["code"], str)
                and issue["code"]
                and isinstance(issue["message"], str)
                and issue["message"]
                and isinstance(issue["severity"], str)
                and issue["severity"] in {"warning", "error"}
                and isinstance(issue["recoverable"], bool)
            ):
                malformed = True
                continue
            issue.setdefault("step_id", step_id)
            normalized.append(issue)
        if malformed:
            normalized.append(
                {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": f"步骤 {step_id} 返回了无效 issues 字段",
                    "severity": "error",
                    "recoverable": False,
                    "stage": "workflow_step",
                    "step_id": step_id,
                }
            )
        return normalized

    @staticmethod
    def _matching_issue(
        issues: List[Dict[str, Any]],
        message: str,
    ) -> Optional[Dict[str, Any]]:
        for index, issue in enumerate(issues):
            if issue.get("message") == message:
                return issues.pop(index)
        return None

    @staticmethod
    def _normalize_steps(steps: Optional[List[Any]]) -> List[Dict[str, Any]]:
        """Legacy helper retained for imports; published v1 configs are not coerced."""
        if not isinstance(steps, list):
            return []
        return [dict(step) for step in steps if isinstance(step, Mapping)]
