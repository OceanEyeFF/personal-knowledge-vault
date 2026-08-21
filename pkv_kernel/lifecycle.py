"""Public, confirmation-bound runtime lifecycle for external PKV Wrappers.

This is the supported way for a Wrapper to decide whether a data root is ready
and, where appropriate, present an explicit setup plan to an operator.  It is
deliberately a façade over the Core runtime implementation: the public handles
serialize only safe DTO fields and never expose Config, RuntimeLayout, Store,
Provider, lease, or RuntimeContext implementation objects.

The handles are process-local and opaque.  ``to_dict()`` is for display or
transport only; callers must not try to reconstruct a plan from that payload.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from src.kernel.facade import (
    KnowledgeKernel,
    configure_kernel as _configure_kernel,
    get_kernel as _get_kernel,
)
from src.runtime.lifecycle import (
    RuntimeConfirmation as _CoreRuntimeConfirmation,
    RuntimeExecution as _CoreRuntimeExecution,
    RuntimeInspection as _CoreRuntimeInspection,
    RuntimePlan as _CoreRuntimePlan,
    confirm_runtime_plan as _confirm_runtime_plan,
    execute_runtime_plan as _execute_runtime_plan,
    inspect_runtime as _inspect_runtime,
    plan_runtime as _plan_runtime,
)
from src.utils.config import Config


__all__ = [
    "RuntimeConfirmation",
    "RuntimeExecution",
    "RuntimeInspection",
    "RuntimePlan",
    "confirm_runtime_plan",
    "execute_runtime_plan",
    "inspect_runtime",
    "open_kernel_from_execution",
    "plan_runtime",
]


_HANDLE_TOKEN = object()


def _safe_payload(value: Any) -> dict[str, object]:
    """Copy a Core serializable DTO without returning its mutable internals."""

    payload = value.to_dict()
    if not isinstance(payload, dict):  # defensive: Core DTO contract violation
        raise TypeError("Core runtime lifecycle returned a non-mapping payload")
    return copy.deepcopy(payload)


class RuntimeInspection:
    """Opaque public result of a side-effect-free runtime inspection."""

    __slots__ = ("__inner",)

    def __init__(self, inner: _CoreRuntimeInspection, *, _token: object | None = None) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError("RuntimeInspection 必须由 pkv_kernel.lifecycle.inspect_runtime 返回")
        self.__inner = inner

    @property
    def readiness(self) -> str:
        return str(_safe_payload(self.__inner)["readiness"])

    @property
    def revision(self) -> str:
        return str(_safe_payload(self.__inner)["revision"])

    def to_dict(self) -> dict[str, object]:
        return _safe_payload(self.__inner)

    def __repr__(self) -> str:
        return f"RuntimeInspection({self.to_dict()!r})"


class RuntimePlan:
    """Opaque, process-local plan bound to one :class:`RuntimeInspection`."""

    __slots__ = ("__inner",)

    def __init__(self, inner: _CoreRuntimePlan, *, _token: object | None = None) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError("RuntimePlan 必须由 pkv_kernel.lifecycle.plan_runtime 返回")
        self.__inner = inner

    @property
    def plan_id(self) -> str:
        return str(_safe_payload(self.__inner)["plan_id"])

    @property
    def inspection(self) -> RuntimeInspection:
        return RuntimeInspection(self.__inner.inspection, _token=_HANDLE_TOKEN)

    def to_dict(self) -> dict[str, object]:
        return _safe_payload(self.__inner)

    def __repr__(self) -> str:
        return f"RuntimePlan({self.to_dict()!r})"


class RuntimeConfirmation:
    """Opaque confirmation made for one public runtime plan."""

    __slots__ = ("__inner",)

    def __init__(self, inner: _CoreRuntimeConfirmation, *, _token: object | None = None) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(
                "RuntimeConfirmation 必须由 pkv_kernel.lifecycle.confirm_runtime_plan 返回"
            )
        self.__inner = inner

    @property
    def plan_id(self) -> str:
        return str(_safe_payload(self.__inner)["plan_id"])

    def to_dict(self) -> dict[str, object]:
        return _safe_payload(self.__inner)

    def __repr__(self) -> str:
        return f"RuntimeConfirmation({self.to_dict()!r})"


class RuntimeExecution:
    """Safe public execution result with an explicit route to a Kernel instance."""

    __slots__ = ("__inner", "__config")

    def __init__(
        self,
        inner: _CoreRuntimeExecution,
        config: Config,
        *,
        _token: object | None = None,
    ) -> None:
        if _token is not _HANDLE_TOKEN:
            raise TypeError(
                "RuntimeExecution 必须由 pkv_kernel.lifecycle.execute_runtime_plan 返回"
            )
        self.__inner = inner
        self.__config = config

    @property
    def inspection(self) -> RuntimeInspection:
        return RuntimeInspection(self.__inner.inspection, _token=_HANDLE_TOKEN)

    def to_dict(self) -> dict[str, object]:
        return _safe_payload(self.__inner)

    def open_kernel(self, *, isolated: bool = False) -> KnowledgeKernel:
        """Compose a Kernel only after lifecycle execution has succeeded.

        ``isolated=False`` (the default) publishes the confirmed snapshot as the
        process-default Kernel.  ``isolated=True`` creates the explicit Config
        B graph required by embedded Wrappers without replacing default Config A.
        In either mode the Config is the private snapshot verified by this exact
        execution; callers cannot substitute a new Config or bypass R2.
        """

        if isolated:
            return _get_kernel(self.__config)
        return _configure_kernel(self.__config)

    def __repr__(self) -> str:
        return f"RuntimeExecution({self.to_dict()!r})"


def _require_config(config: Config | None) -> Config | None:
    if config is not None and not isinstance(config, Config):
        raise TypeError("config 必须是 pkv_kernel.Config 或 None")
    return config


def inspect_runtime(config: Config | None = None) -> RuntimeInspection:
    """Inspect the selected runtime without creating files or probing Providers."""

    return RuntimeInspection(
        _inspect_runtime(_require_config(config)),
        _token=_HANDLE_TOKEN,
    )


def plan_runtime(inspection: RuntimeInspection) -> RuntimePlan:
    """Create a side-effect-free, process-local plan for an inspection."""

    if not isinstance(inspection, RuntimeInspection):
        raise TypeError("inspection 必须由 pkv_kernel.lifecycle.inspect_runtime 返回")
    return RuntimePlan(
        _plan_runtime(inspection._RuntimeInspection__inner),
        _token=_HANDLE_TOKEN,
    )


def confirm_runtime_plan(
    plan: RuntimePlan,
    *,
    allow_network: bool = False,
    approved_actions: Iterable[str] | None = None,
) -> RuntimeConfirmation:
    """Confirm exactly one plan; network use remains opt-in by default."""

    if not isinstance(plan, RuntimePlan):
        raise TypeError("plan 必须由 pkv_kernel.lifecycle.plan_runtime 返回")
    if type(allow_network) is not bool:
        raise TypeError("allow_network 必须是 bool")
    normalized_actions = None
    if approved_actions is not None:
        normalized_actions = frozenset(str(action) for action in approved_actions)
    return RuntimeConfirmation(
        _confirm_runtime_plan(
            plan._RuntimePlan__inner,
            allow_network=allow_network,
            approved_actions=normalized_actions,
        ),
        _token=_HANDLE_TOKEN,
    )


def execute_runtime_plan(
    plan: RuntimePlan,
    confirmation: RuntimeConfirmation | None = None,
) -> RuntimeExecution:
    """Execute an approved plan using the Core's default lease/probe policy.

    This façade intentionally offers no Provider, lease, path, or fake injection
    parameters.  Such seams belong to isolated Core tests, not external Wrapper
    code.  Missing confirmation or network permission projects the stable
    ``PKVRuntimeError`` codes from the runtime contract.
    """

    if not isinstance(plan, RuntimePlan):
        raise TypeError("plan 必须由 pkv_kernel.lifecycle.plan_runtime 返回")
    if confirmation is not None and not isinstance(confirmation, RuntimeConfirmation):
        raise TypeError(
            "confirmation 必须由 pkv_kernel.lifecycle.confirm_runtime_plan 返回"
        )
    core_execution = _execute_runtime_plan(
        plan._RuntimePlan__inner,
        (
            None
            if confirmation is None
            else confirmation._RuntimeConfirmation__inner
        ),
    )
    config = core_execution._config
    if not isinstance(config, Config):
        raise RuntimeError("已执行的运行时计划没有可公开组合的 Config 快照")
    return RuntimeExecution(core_execution, config, _token=_HANDLE_TOKEN)


def open_kernel_from_execution(
    execution: RuntimeExecution,
    *,
    isolated: bool = False,
) -> KnowledgeKernel:
    """Convenience equivalent of :meth:`RuntimeExecution.open_kernel`."""

    if not isinstance(execution, RuntimeExecution):
        raise TypeError(
            "execution 必须由 pkv_kernel.lifecycle.execute_runtime_plan 返回"
        )
    return execution.open_kernel(isolated=isolated)
