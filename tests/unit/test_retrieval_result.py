"""
SearchResult 数据类单元测试
"""

import pytest

from src.retrieval.result import SearchResponse, SearchResult


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


def test_search_response_preserves_list_like_behavior():
    """SearchResponse 应能表达状态，同时兼容旧列表调用方式。"""
    item = SearchResult(
        knowledge_id=1,
        title="测试",
        score=0.8,
        highlight="摘要",
        metadata={},
    )
    response = SearchResponse(results=[item], status="success")

    assert response.ok is True
    assert response.failed is False
    assert len(response) == 1
    assert list(response) == [item]
    assert response[0] == item
    assert response[:1] == [item]
    assert response != []
    assert response == SearchResponse(results=[item], status="success")
    assert response != SearchResponse(results=[], status="no_results")
    assert response != object()


def test_search_response_distinguishes_error_from_no_results():
    """空结果和检索错误都可保持旧列表兼容，但状态不同。"""
    no_results = SearchResponse(results=[], status="no_results")
    failed = SearchResponse(
        results=[],
        status="error",
        error_message="fts unavailable",
        error_type="OperationalError",
    )

    assert no_results == []
    assert no_results.ok is True
    assert no_results.failed is False
    assert failed == []
    assert failed.ok is False
    assert failed.failed is True
    assert failed.error_type == "OperationalError"
