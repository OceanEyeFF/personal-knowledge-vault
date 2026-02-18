"""
MCP 服务模块

将 Personal Knowledge Vault 作为 MCP Server 暴露给 AI Agent（Claude Code、Cursor 等），
使 AI 能够直接搜索、检索、归档和浏览知识库。

模块结构:
    - server.py: FastMCP 主入口
    - tools.py: Tool handler 实现
    - resources.py: Resource handler 实现
    - utils.py: 辅助工具（序列化、错误处理）
"""
