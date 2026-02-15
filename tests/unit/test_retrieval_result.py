"""
SearchResult 数据类单元测试
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest

from src.retrieval.result import SearchResult


def test_search_result_creation():
    """测试创建 SearchResult"""
    result = SearchResult(
        knowledge_id=1,
        title="测试标题",
        score=0.95,
        highlight="这是一个测试摘要",
        metadata={"source_type": "webpage", "tags": "测试"},
    )

    assert result.knowledge_id == 1
    assert result.title == "测试标题"
    assert result.score == 0.95
    assert result.highlight == "这是一个测试摘要"
    assert result.metadata["source_type"] == "webpage"


def test_search_result_frozen():
    """测试 SearchResult 是不可变的"""
    result = SearchResult(
        knowledge_id=1,
        title="测试",
        score=0.8,
        highlight="摘要",
        metadata={},
    )

    # 尝试修改属性应该抛出异常
    with pytest.raises(AttributeError):
        result.score = 0.9


def test_search_result_score_validation():
    """测试分数范围验证"""
    # 正常范围
    result = SearchResult(
        knowledge_id=1, title="测试", score=0.5, highlight="摘要", metadata={}
    )
    assert result.score == 0.5

    # 超出范围应该抛出异常
    with pytest.raises(ValueError):
        SearchResult(
            knowledge_id=1, title="测试", score=1.5, highlight="摘要", metadata={}
        )

    with pytest.raises(ValueError):
        SearchResult(
            knowledge_id=1, title="测试", score=-0.1, highlight="摘要", metadata={}
        )
