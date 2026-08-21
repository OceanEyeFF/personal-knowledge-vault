"""
PKV MCP Server 主入口

创建 FastMCP 实例，注册 Tool/Resource/Prompt handler。M13 发布面仅包含 stdio。

启动方式:
    # stdio 模式（Claude Code / Cursor 本地集成）
    python -m src.mcp.server

    # MCP Inspector 可视化测试
    npx @modelcontextprotocol/inspector python -m src.mcp.server
"""

import json
import logging
from dataclasses import dataclass
from threading import RLock
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.application import configure_application, get_application as _application_get_application
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.lifecycle import (
    RuntimeInspection,
    RuntimeReadiness,
    inspect_runtime,
    plan_runtime,
)
from src.runtime.write_lease import has_active_write_lease
from src.utils.config import get_config
from src.utils.logger import LoggerSetup


_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


def _canonical_log_level(value: object) -> tuple[str, int]:
    """Map untrusted config input to one published logging level."""

    if type(value) is str:
        normalized = value.upper()
        if normalized in _LOG_LEVELS:
            return normalized, _LOG_LEVELS[normalized]
    return "INFO", logging.INFO


def _exception_type_for_log(exc: BaseException) -> str:
    """Return a bounded exception category without trusting ``__name__``."""

    if isinstance(exc, OSError):
        return "OSError"
    if isinstance(exc, ValueError):
        return "ValueError"
    if isinstance(exc, TypeError):
        return "TypeError"
    return "Exception"


logger = logging.getLogger("pkv.mcp")


@dataclass
class _MCPRuntimeState:
    """Per-process lifecycle state owned by the stdio adapter.

    A module imported by unit/in-process tests has no stdio lifecycle yet, so
    it deliberately remains in compatibility mode.  ``main()`` switches it to
    managed mode before accepting any protocol request; thereafter every
    service accessor re-inspects the captured immutable Config snapshot before
    constructing an application graph.
    """

    config: Any | None = None
    inspection: RuntimeInspection | None = None
    startup_error: PKVRuntimeError | None = None
    managed: bool = False
    configured_application: bool = False


_runtime_state = _MCPRuntimeState()
_runtime_state_lock = RLock()


def _set_runtime_state(
    *,
    config: Any | None,
    inspection: RuntimeInspection | None,
    startup_error: PKVRuntimeError | None = None,
    managed: bool,
) -> None:
    """Publish one adapter-local state without initializing the product graph."""

    global _runtime_state
    with _runtime_state_lock:
        _runtime_state = _MCPRuntimeState(
            config=config,
            inspection=inspection,
            startup_error=startup_error,
            managed=managed,
            configured_application=False,
        )


def _readiness_error(inspection: RuntimeInspection) -> PKVRuntimeError:
    """Turn a non-ready inspection into a stable, retryable MCP gate error."""

    if inspection.readiness is RuntimeReadiness.SETUP_REQUIRED:
        code = ErrorCode.SETUP_REQUIRED
        message = "知识库尚未完成显式初始化。"
    elif inspection.readiness is RuntimeReadiness.UPGRADE_REQUIRED:
        code = ErrorCode.DATABASE_UPGRADE_REQUIRED
        message = "知识库需要显式升级后才能使用。"
    else:
        code = ErrorCode.REPAIR_REQUIRED
        message = "知识库需要先完成显式修复。"
    return PKVRuntimeError(
        code,
        message,
        stage="runtime_readiness",
        recoverable=True,
    )


def _refresh_runtime_inspection() -> RuntimeInspection:
    """Re-inspect the managed runtime without creating/recovering anything."""

    with _runtime_state_lock:
        state = _runtime_state
    if state.startup_error is not None:
        raise state.startup_error
    if state.config is None:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "运行时配置不可用，需要先修复。",
            stage="runtime_readiness",
            recoverable=True,
        )
    try:
        inspection = inspect_runtime(state.config)
    except PKVRuntimeError:
        raise
    except Exception as exc:
        raise PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "运行时状态无法安全确认，需要先修复。",
            stage="runtime_readiness",
            recoverable=True,
        ) from exc
    with _runtime_state_lock:
        # Do not replace the captured Config during a request: an in-flight
        # handler must use one snapshot even if a later CLI process writes user
        # settings.  The newly inspected state is nevertheless observable to
        # the status tool and gates the next backend access.
        if _runtime_state.managed and _runtime_state.config is state.config:
            _runtime_state.inspection = inspection
    return inspection


def get_runtime_status_payload() -> dict[str, object]:
    """Return the read-only lifecycle status exposed by ``get_runtime_status``.

    This function never invokes ``bootstrap_runtime``, Provider probes, a
    recovery routine, or an application accessor.  It remains callable when a
    server starts against an unready or malformed runtime.
    """

    with _runtime_state_lock:
        state = _runtime_state
    if not state.managed:
        try:
            config = get_config()
            inspection = inspect_runtime(config)
        except Exception:
            return {
                "status": "error",
                "readiness": RuntimeReadiness.REPAIR_REQUIRED.value,
                "inspection": None,
                "plan": None,
                "issues": [
                    {
                        "code": ErrorCode.REPAIR_REQUIRED.value,
                        "message": "运行时状态无法安全确认，需要先修复。",
                        "stage": "runtime_readiness",
                        "recoverable": True,
                    }
                ],
            }
    else:
        try:
            inspection = _refresh_runtime_inspection()
        except PKVRuntimeError as exc:
            return {
                "status": "error",
                "readiness": RuntimeReadiness.REPAIR_REQUIRED.value,
                "inspection": None,
                "plan": None,
                "issues": [
                    {
                        "code": exc.code.value,
                        "message": "运行时状态无法安全确认，需要先修复。",
                        "stage": "runtime_readiness",
                        "recoverable": exc.recoverable,
                    }
                ],
            }
    try:
        plan = plan_runtime(inspection)
    except Exception:
        return {
            "status": "error",
            "readiness": RuntimeReadiness.REPAIR_REQUIRED.value,
            "inspection": inspection.to_dict(),
            "plan": None,
            "issues": [
                {
                    "code": ErrorCode.REPAIR_REQUIRED.value,
                    "message": "运行时计划无法安全生成，需要先修复。",
                    "stage": "runtime_readiness",
                    "recoverable": True,
                }
            ],
        }
    return {
        "status": "success",
        "readiness": inspection.readiness.value,
        "inspection": inspection.to_dict(),
        "plan": plan.to_dict(),
        "issues": [issue.to_dict() for issue in inspection.issues],
    }

# ============================================================
# FastMCP 实例（模块级单例）
# ============================================================

mcp = FastMCP(
    name="Personal Knowledge Vault",
    instructions=(
        "个人知识库 MCP 服务。支持知识搜索、归档、浏览、关联推荐、关系推理和统计。\n"
        "只读工具：get_runtime_status（运行时状态）、search_knowledge（搜索）、get_entry（查看详情）、"
        "list_tags（标签列表）、list_entries（浏览条目）、get_stats（统计）、"
        "get_related（关联推荐）、query_subgraph（关系子图）、"
        "explain_relation（关系解释）、collect_evidence（证据聚合）、"
        "find_bridges（桥接发现）、timeline_of（弱时间线）、"
        "contrast（主题对比）。\n"
        "写入工具：archive_url（归档网页）、archive_text（归档文本）。\n"
        "Prompt 模板：search_and_summarize、knowledge_qa、idea_sharpen。\n"
        "知识条目包含标题、摘要、标签、全文等信息。"
    ),
)


# ============================================================
# 应用服务访问器
#
# MCP 是协议适配层，不在这里拼装 Store、Retriever、Provider、Workflow 或
# relation service。所有长生命周期依赖都由 ``KnowledgeApplication`` 延迟创建并
# 复用；下列兼容访问器只保留给已注册的 tool/resource 模块和测试 seam。
# ============================================================

def get_application():
    """Return the application only when the managed MCP runtime is READY.

    In-process unit/integration callers that import the module without calling
    ``main`` retain the historical accessor seam.  A real stdio process first
    enters managed mode in ``main`` and is then fail-closed: a non-ready root
    can expose only ``get_runtime_status`` and cannot lazily initialize an
    application, database, journal, Provider, or log file through a Tool.
    """

    with _runtime_state_lock:
        state = _runtime_state
    if not state.managed:
        return _application_get_application()

    inspection = _refresh_runtime_inspection()
    if inspection.readiness is not RuntimeReadiness.READY:
        raise _readiness_error(inspection)

    with _runtime_state_lock:
        state = _runtime_state
        if state.config is None:
            raise PKVRuntimeError(
                ErrorCode.REPAIR_REQUIRED,
                "运行时配置不可用，需要先修复。",
                stage="runtime_readiness",
                recoverable=True,
            )
        if not state.configured_application:
            # Composition itself is side-effect free.  It is deliberately
            # deferred until a READY service request rather than happening at
            # MCP startup, so an unready server remains status-only.
            configure_application(state.config)
            _runtime_state.configured_application = True
        config = state.config
    # ``configure_application`` publishes the captured Config snapshot as the
    # one process-default graph.  Passing it back as an explicit argument would
    # deliberately create a fresh isolated application for every Tool call,
    # losing long-lived caches and defeating the MCP process snapshot contract.
    return _application_get_application()


def get_sqlite_store():
    """Return the shared application's SQLite store.

    Kept as a narrow compatibility accessor so handlers do not need to know
    how a store is composed.
    """

    return get_application().sqlite_store


def get_markdown_store():
    """Return the shared application's Markdown store."""

    return get_application().markdown_store


def get_query_router():
    """Return the shared application's lazy query router."""

    return get_application().query_router()


def get_relation_query_service():
    """Return the shared application's relation query service."""

    return get_application().relation_query_service


def get_evidence_collection_service():
    """Return the shared application's evidence collection service."""

    return get_application().evidence_collection_service


def get_exploration_service():
    """Return the shared application's exploration service."""

    return get_application().exploration_service


# ============================================================
# 注册 Tool / Resource / Prompt handler（通过导入副作用完成注册）
# ============================================================

# 延迟导入：在 mcp 实例创建后再导入子模块，触发装饰器注册
# ⚠️ 修复 python -m src.mcp.server 的 "double import" 问题：
# 当直接运行本文件时 __name__ == "__main__"，但子模块中
# from src.mcp.server import mcp 会创建另一个 src.mcp.server 副本。
# 在导入子模块前将自身注册为 src.mcp.server，确保引用同一实例。
import sys as _sys  # noqa: E402
_sys.modules.setdefault("src.mcp.server", _sys.modules[__name__])

from src.mcp import tools  # noqa: E402, F401
from src.mcp import resources  # noqa: E402, F401
from src.mcp import prompts  # noqa: E402, F401


def ensure_supported_transport(transport: str) -> None:
    """Reject unpublished transports before config/bootstrap or socket binding."""

    if type(transport) is not str or transport != "stdio":
        raise PKVRuntimeError(
            ErrorCode.TRANSPORT_UNSUPPORTED,
            "M13 仅发布 stdio transport",
            stage="mcp_transport_selection",
            recoverable=False,
        )


def main():
    """CLI 入口：启动 MCP 服务。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="PKV MCP Server")
    parser.add_argument(
        "--transport",
        default="stdio",
        help="传输方式（M13 仅支持 stdio，默认: stdio）",
    )
    # Parse the historic flag so an old command receives the stable transport
    # rejection instead of failing on an unrelated unknown-argument message.
    parser.add_argument(
        "--port",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--log-level",
        default=None,
        help="日志级别 (覆盖 config.yaml 和环境变量)",
    )
    args = parser.parse_args()

    if args.log_level is not None and args.log_level not in _LOG_LEVELS:
        parser.error("日志级别无效；可选 DEBUG、INFO、WARNING、ERROR")

    try:
        ensure_supported_transport(args.transport)
    except PKVRuntimeError as exc:
        parser.error(f"{exc.code.value}: {exc}")

    # A stdio server must be usable as a read-only status endpoint even when a
    # data root still needs setup or repair.  In particular, do not call the
    # historical mutating bootstrap here: startup performs only Config parsing
    # plus a readonly lifecycle inspection.  Backend accessors re-inspect again
    # before serving any non-status Tool/Resource request.
    stage = "runtime_configuration"
    config = None
    inspection: RuntimeInspection | None = None
    startup_error: PKVRuntimeError | None = None
    inspection_failure_log: tuple[str, str] | None = None
    try:
        config = get_config()
        stage = "runtime_inspection"
        inspection = inspect_runtime(config)
    except PKVRuntimeError as exc:
        startup_error = exc
        _set_runtime_state(
            config=config,
            inspection=None,
            startup_error=startup_error,
            managed=True,
        )
    except Exception as exc:
        # Capture only bounded diagnostic fields while an untrusted
        # configuration exception is active.  Emit the log after the exception
        # handler so a secondary stderr/handler failure cannot render the
        # original exception context (which may contain a config path or key).
        inspection_failure_log = (_exception_type_for_log(exc), stage)
        startup_error = PKVRuntimeError(
            ErrorCode.REPAIR_REQUIRED,
            "运行时状态无法安全确认，需要先修复。",
            stage="runtime_readiness",
            recoverable=True,
        )
        _set_runtime_state(
            config=config,
            inspection=None,
            startup_error=startup_error,
            managed=True,
        )
    else:
        _set_runtime_state(
            config=config,
            inspection=inspection,
            managed=True,
        )

    if inspection_failure_log is not None:
        cause_type, failure_stage = inspection_failure_log
        logger.warning(
            "MCP runtime inspection unavailable: cause_type=%s stage=%s",
            cause_type,
            failure_stage,
        )

    # ── 日志级别优先级: --log-level > config.yaml/PKV_LOG_LEVEL > INFO ──
    configured_level = (
        args.log_level
        if args.log_level
        else getattr(config, "log_level", "INFO")
    )
    level_str, log_level = _canonical_log_level(configured_level)

    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    formatter = logging.Formatter(log_format)

    # 获取根 logger，避免重复 handler
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    # ── 控制台 handler ──
    # ⚠️ stdio 模式下 stdout 被 MCP 协议占用，日志必须走 stderr
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # ── 文件 handler（仅 READY 的 config）──
    # 日志叶子和 CLI 一样走统一可写叶子合同；失败不阻止服务启动。
    # 配置边界只接受 YAML boolean；不要对字符串、数字或自定义对象执行
    # 隐式真值转换（后者甚至可能在 ``__bool__`` 中产生副作用）。
    file_enabled = (
        config.get("logging.file.enabled", True)
        if config is not None and hasattr(config, "get")
        else False
    )
    if (
        inspection is not None
        and inspection.readiness is RuntimeReadiness.READY
        and file_enabled is True
    ):
        log_file = config.log_dir / "pkv.log"
        try:
            LoggerSetup.add_file_handler(
                log_file,
                path_validator=config.layout.writable_user_path,
                level=log_level,
                log_format=log_format,
                # A running MCP server can serve concurrent read requests.
                # Its durable log must therefore be a mutation only while the
                # current request owns this data-root's writer lease.  The
                # console handler remains available for all diagnostics.
                delay=True,
                emit_guard=lambda: has_active_write_lease(config.layout),
            )
            logger.info("MCP 文件日志初始化完成")
        except Exception as e:
            # 文件日志初始化失败不应阻止服务启动
            logger.warning(
                "MCP 文件日志初始化失败: cause_type=%s",
                _exception_type_for_log(e),
            )

    readiness = (
        inspection.readiness.value
        if inspection is not None
        else RuntimeReadiness.REPAIR_REQUIRED.value
    )
    logger.info(
        "启动 PKV MCP Server (transport=stdio, log_level=%s, readiness=%s)",
        level_str,
        readiness,
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
