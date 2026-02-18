"""
MCP Prompt 模板

提供 3 个预定义的知识库交互提示词模板。
用户在 MCP 客户端 UI 中选择模板 → 填入参数 → 模板生成引导文本 → 客户端发给 AI。
AI 根据引导文本自行决定调用哪些 Tool。

注意: Prompt handler 是同步 def（返回 str），不需要 async/await。
"""

from src.mcp.server import mcp


# ============================================================
# Prompt 1: search_and_summarize — 搜索并总结
# ============================================================

@mcp.prompt()
def search_and_summarize(query: str, context: str = "") -> str:
    """搜索知识库并总结结果。

    先使用 search_knowledge 工具搜索，然后总结找到的内容。

    Args:
        query: 搜索关键词或主题
        context: 可选的背景信息，帮助 AI 理解搜索意图
    """
    base = f"请在我的知识库中搜索关于「{query}」的内容。"
    if context:
        base += f"\n\n背景信息：{context}"
    base += (
        "\n\n请执行以下步骤：\n"
        "1. 使用 search_knowledge 工具搜索相关内容\n"
        "2. 对搜索结果进行总结归纳\n"
        "3. 如果找到多个相关条目，说明它们之间的关系\n"
        "4. 指出最相关的 1-3 条内容的标题和关键信息"
    )
    return base


# ============================================================
# Prompt 2: knowledge_qa — 知识库问答
# ============================================================

@mcp.prompt()
def knowledge_qa(question: str) -> str:
    """基于知识库的智能问答。

    利用知识库中的内容回答用户问题。

    Args:
        question: 用户的问题
    """
    return (
        f"请基于我的个人知识库回答以下问题：\n\n"
        f"**问题**：{question}\n\n"
        f"请执行以下步骤：\n"
        f"1. 使用 search_knowledge 搜索可能相关的知识条目\n"
        f"2. 如果找到相关内容，使用 get_entry 获取详细信息\n"
        f"3. 基于知识库中的内容给出回答\n"
        f"4. 如果知识库中没有相关信息，明确告知\n"
        f"5. 引用具体的知识条目标题和来源"
    )


# ============================================================
# Prompt 3: idea_sharpen — 思想磨砺
# ============================================================

@mcp.prompt()
def idea_sharpen(content: str, entry_id: str = "") -> str:
    """对知识条目进行 idea Sharpen（思想磨砺）对话。

    帮助用户深入思考某个知识条目的核心价值和应用场景。

    Args:
        content: 要讨论的内容文本（最多取前 2000 字符）
        entry_id: 可选的知识条目 ID，AI 可据此拉取完整条目
    """
    # 截取前 2000 字符避免 Prompt 过长
    content_preview = content[:2000] if len(content) > 2000 else content

    base = (
        f"让我们对以下内容进行 idea Sharpen（思想磨砺）：\n\n"
        f"**内容**：\n{content_preview}\n\n"
    )
    if entry_id:
        base += f"（知识条目 ID：{entry_id}，可使用 get_entry 获取完整内容）\n\n"
    base += (
        "请帮我深入思考以下问题：\n"
        "1. 这篇内容的**核心价值**是什么？\n"
        "2. 有哪些**关键观点**值得记住？\n"
        "3. 与我知识库中的其他内容有什么**关联**？（请使用 search_knowledge 搜索）\n"
        "4. 这些知识可以如何**应用**到实际场景中？\n\n"
        "如果有关联条目，请使用 get_related 获取推荐，并建立知识关联。"
    )
    return base
