"""
PKV MCP Server 主入口

创建 FastMCP 实例，注册 Tool/Resource/Prompt handler，提供 stdio 和 streamable-http 传输方式。

启动方式:
    # stdio 模式（Claude Code / Cursor 本地集成）
    python -m src.mcp.server

    # HTTP 模式（远程访问，M9 完善认证后推荐）
    python -m src.mcp.server --transport streamable-http --port 3000

    # MCP Inspector 可视化测试
    npx @modelcontextprotocol/inspector python -m src.mcp.server
"""

import logging

from mcp.server.fastmcp import FastMCP

from src.runtime.bootstrap import bootstrap_runtime
from src.utils.config import get_config
from src.utils.logger import LoggerSetup

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
# 服务对象单例管理（⚠️ 关键架构决策）
#
# 以下对象在首次访问时延迟初始化，整个 Server 生命周期内复用：
# - SQLiteStore：数据库连接复用
# - MarkdownStore：Markdown 文件读取
# - QueryRouter：内含 BM25 + HybridRetriever + VectorStore（hnswlib 索引加载耗时 ~1-3s）
#
# 为什么不能每次请求重建？
# - VectorRetriever 需要加载 hnswlib 索引文件到内存
# - QueryRouter 内部创建多个 Retriever 实例，重复创建浪费资源
# ============================================================

_sqlite_store = None
_markdown_store = None
_query_router = None
_relation_query_service = None
_evidence_collection_service = None
_exploration_service = None


def get_sqlite_store():
    """获取 SQLiteStore 单例。

    Returns:
        SQLiteStore 实例
    """
    global _sqlite_store
    if _sqlite_store is None:
        from src.storage.sqlite_store import SQLiteStore
        config = get_config()
        _sqlite_store = SQLiteStore(config.db_path)
        logger.info(f"SQLiteStore 单例初始化: {config.db_path}")
    return _sqlite_store


def get_markdown_store():
    """获取 MarkdownStore 单例。

    Returns:
        MarkdownStore 实例
    """
    global _markdown_store
    if _markdown_store is None:
        from src.storage.markdown_store import MarkdownStore
        config = get_config()
        _markdown_store = MarkdownStore(config.vault_dir)
        logger.info(f"MarkdownStore 单例初始化: {config.vault_dir}")
    return _markdown_store


def get_query_router():
    """获取 QueryRouter 单例（内含 BM25 + HybridRetriever + VectorStore）。

    Returns:
        QueryRouter 实例
    """
    global _query_router
    if _query_router is None:
        from src.retrieval.query_router import QueryRouter
        from src.ai.openai_client import OpenAIClient
        config = get_config()
        embedder = OpenAIClient(config)
        _query_router = QueryRouter(
            db_path=config.db_path,
            vector_index_dir=config.vector_index_dir,
            embedder=embedder,
        )
        logger.info("QueryRouter 单例初始化完成")
    return _query_router


def get_relation_query_service():
    """获取 RelationQueryService 单例。"""
    global _relation_query_service
    if _relation_query_service is None:
        from src.relations.query_service import RelationQueryService
        from src.storage.relation_store import RelationStore

        config = get_config()
        relation_store = RelationStore(config.db_path)
        _relation_query_service = RelationQueryService(relation_store)
        logger.info("RelationQueryService 单例初始化完成")
    return _relation_query_service


def get_evidence_collection_service():
    """获取 EvidenceCollectionService 单例。"""
    global _evidence_collection_service
    if _evidence_collection_service is None:
        from src.relations.evidence_service import EvidenceCollectionService

        _evidence_collection_service = EvidenceCollectionService(
            query_router=get_query_router(),
            sqlite_store=get_sqlite_store(),
            markdown_store=get_markdown_store(),
            relation_query_service=get_relation_query_service(),
        )
        logger.info("EvidenceCollectionService 单例初始化完成")
    return _evidence_collection_service


def get_exploration_service():
    """获取 ExplorationService 单例。"""
    global _exploration_service
    if _exploration_service is None:
        from src.relations.exploration_service import ExplorationService

        _exploration_service = ExplorationService(
            query_router=get_query_router(),
            sqlite_store=get_sqlite_store(),
            relation_query_service=get_relation_query_service(),
            vault_dir=get_markdown_store().vault_dir,
        )
        logger.info("ExplorationService 单例初始化完成")
    return _exploration_service


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


def main():
    """CLI 入口：启动 MCP 服务。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="PKV MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="传输方式 (默认: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help="HTTP 端口 (仅 streamable-http 模式, 默认: 3000)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="日志级别 (覆盖 config.yaml 和环境变量)",
    )
    args = parser.parse_args()

    # 所有 adapter 共用同一个路径/数据库启动门禁。
    config = get_config()
    bootstrap_runtime(config)
    # ── 日志级别优先级: --log-level > LOG_LEVEL 环境变量 > config.yaml > INFO ──
    if args.log_level:
        level_str = args.log_level
    else:
        level_str = config.log_level  # 内部已处理 LOG_LEVEL 环境变量 → config.yaml → "INFO"
    log_level = getattr(logging, level_str.upper(), logging.INFO)

    log_format = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    formatter = logging.Formatter(log_format)

    # 获取根 logger，避免重复 handler
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    # ── 控制台 handler ──
    # ⚠️ stdio 模式下 stdout 被 MCP 协议占用，日志必须走 stderr
    # HTTP 模式下同样使用 stderr（与大多数 server 惯例一致）
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # ── 文件 handler（读取 config.yaml 的 logging.file 配置）──
    # 日志叶子和 CLI/GUI 一样走统一可写叶子合同；失败不阻止服务启动。
    file_enabled = config.get("logging.file.enabled", True)
    if file_enabled:
        log_file = config.log_dir / "pkv.log"
        try:
            LoggerSetup.add_file_handler(
                log_file,
                path_validator=config.layout.writable_user_path,
                level=log_level,
                log_format=log_format,
            )
            logger.info(f"日志文件: {log_file}")
        except Exception as e:
            # 文件日志初始化失败不应阻止服务启动
            logger.warning(f"日志文件初始化失败 ({log_file}): {e}")

    logger.info(f"启动 PKV MCP Server (transport={args.transport}, log_level={level_str})")

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            port=args.port,
        )


if __name__ == "__main__":
    main()
