"""
MCP Prompts 单元测试

测试 3 个 Prompt 模板的参数处理和输出格式。
"""

import sys
from pathlib import Path

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp.prompts import search_and_summarize, knowledge_qa, idea_sharpen


# ============================================================
# search_and_summarize 测试
# ============================================================

class TestSearchAndSummarize:
    """search_and_summarize Prompt 测试。"""

    def test_basic_query(self):
        result = search_and_summarize("AI 工作流")
        assert "AI 工作流" in result
        assert "search_knowledge" in result

    def test_with_context(self):
        result = search_and_summarize("AI 工作流", context="我正在研究自动化流程")
        assert "AI 工作流" in result
        assert "自动化流程" in result
        assert "背景信息" in result

    def test_empty_context_ignored(self):
        result = search_and_summarize("AI", context="")
        assert "背景信息" not in result

    def test_returns_string(self):
        result = search_and_summarize("test")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_step_instructions(self):
        result = search_and_summarize("test")
        assert "1." in result
        assert "2." in result


# ============================================================
# knowledge_qa 测试
# ============================================================

class TestKnowledgeQA:
    """knowledge_qa Prompt 测试。"""

    def test_basic_question(self):
        result = knowledge_qa("什么是向量检索？")
        assert "什么是向量检索" in result
        assert "search_knowledge" in result

    def test_contains_get_entry_instruction(self):
        result = knowledge_qa("test question")
        assert "get_entry" in result

    def test_contains_citation_instruction(self):
        result = knowledge_qa("test")
        assert "引用" in result
        assert "标题" in result

    def test_returns_string(self):
        result = knowledge_qa("any question")
        assert isinstance(result, str)
        assert len(result) > 0


# ============================================================
# idea_sharpen 测试
# ============================================================

class TestIdeaSharpen:
    """idea_sharpen Prompt 测试。"""

    def test_basic_content(self):
        result = idea_sharpen("这是一段关于知识管理的内容")
        assert "知识管理" in result
        assert "核心价值" in result

    def test_with_entry_id(self):
        result = idea_sharpen("内容", entry_id="42")
        assert "42" in result
        assert "get_entry" in result

    def test_without_entry_id(self):
        result = idea_sharpen("内容", entry_id="")
        # entry_id 为空时不应出现条目 ID 提示
        assert "知识条目 ID" not in result

    def test_content_truncation(self):
        """长内容应被截断到 2000 字符。"""
        long_content = "A" * 5000
        result = idea_sharpen(long_content)
        # 生成的 Prompt 中内容部分不应包含完整 5000 字符
        # 但应包含前 2000 个字符
        assert "A" * 100 in result
        # Prompt 总长度应远小于 5000 + 模板文本
        assert len(result) < 5000

    def test_contains_search_instruction(self):
        result = idea_sharpen("内容")
        assert "search_knowledge" in result
        assert "get_related" in result

    def test_returns_string(self):
        result = idea_sharpen("test content")
        assert isinstance(result, str)
        assert len(result) > 0
