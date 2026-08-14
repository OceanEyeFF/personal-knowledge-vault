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

from mcp.server.fastmcp import FastMCP

from src.application import configure_application, get_application
from src.runtime.bootstrap import bootstrap_runtime, project_bootstrap_error
from src.runtime.errors import ErrorCode, PKVRuntimeError
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

# ============================================================
# FastMCP 实例（模块级单例）
# ============================================================

mcp = FastMCP(
    name="Personal Knowledge Vault",
    instructions=(
        "个人知识库 MCP 服务。支持知识搜索、归档、浏览、关联推荐、关系推理和统计。\n"
        "只读工具：search_knowledge（搜索）、get_entry（查看详情）、"
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

    # 所有 adapter 共用同一个路径/数据库启动门禁。
    stage = "runtime_configuration"
    try:
        config = get_config()
        stage = "runtime_bootstrap"
        bootstrap_runtime(config)
        configure_application(config)
    except PKVRuntimeError as exc:
        sys.stderr.write(
            json.dumps(
                project_bootstrap_error(exc, adapter="mcp"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        raise SystemExit(1) from None
    except Exception as exc:
        sys.stderr.write(
            json.dumps(
                project_bootstrap_error(exc, adapter="mcp", stage=stage),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        raise SystemExit(1) from None
    # ── 日志级别优先级: --log-level > LOG_LEVEL 环境变量 > config.yaml > INFO ──
    configured_level = args.log_level if args.log_level else config.log_level
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

    # ── 文件 handler（读取 config.yaml 的 logging.file 配置）──
    # 日志叶子和 CLI 一样走统一可写叶子合同；失败不阻止服务启动。
    # 配置边界只接受 YAML boolean；不要对字符串、数字或自定义对象执行
    # 隐式真值转换（后者甚至可能在 ``__bool__`` 中产生副作用）。
    file_enabled = config.get("logging.file.enabled", True)
    if file_enabled is True:
        log_file = config.log_dir / "pkv.log"
        try:
            LoggerSetup.add_file_handler(
                log_file,
                path_validator=config.layout.writable_user_path,
                level=log_level,
                log_format=log_format,
            )
            logger.info("MCP 文件日志初始化完成")
        except Exception as e:
            # 文件日志初始化失败不应阻止服务启动
            logger.warning(
                "MCP 文件日志初始化失败: cause_type=%s",
                _exception_type_for_log(e),
            )

    logger.info(f"启动 PKV MCP Server (transport=stdio, log_level={level_str})")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
