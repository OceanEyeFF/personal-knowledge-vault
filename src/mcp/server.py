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
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from src.utils.config import get_config

logger = logging.getLogger("pkv.mcp")

# ============================================================
# FastMCP 实例（模块级单例）
# ============================================================

mcp = FastMCP(
    name="Personal Knowledge Vault",
    instructions=(
        "个人知识库 MCP 服务。支持知识搜索、归档、浏览和统计。\n"
        "可用工具：search_knowledge（搜索）、get_entry（查看详情）、"
        "list_tags（标签列表）、list_entries（浏览条目）、get_stats（统计信息）。\n"
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


# ============================================================
# 注册 Tool / Resource handler（通过导入副作用完成注册）
# ============================================================

# 延迟导入：在 mcp 实例创建后再导入子模块，触发 @mcp.tool() / @mcp.resource() 注册
from src.mcp import tools  # noqa: E402, F401
from src.mcp import resources  # noqa: E402, F401


def main():
    """CLI 入口：启动 MCP 服务。"""
    import argparse

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
    args = parser.parse_args()

    # 配置日志（stdio 模式下 stdout 被 MCP 协议占用，日志只能到 stderr）
    log_level = logging.INFO
    if args.transport == "stdio":
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            handlers=[logging.StreamHandler()],  # stderr
        )
    else:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        )

    logger.info(f"启动 PKV MCP Server (transport={args.transport})")

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(
            transport="streamable-http",
            port=args.port,
        )


if __name__ == "__main__":
    main()
