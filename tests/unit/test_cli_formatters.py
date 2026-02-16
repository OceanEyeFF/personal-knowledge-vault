"""
CLI formatter unit tests.
"""

import sys
import json
from dataclasses import dataclass
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest

from src.cli.formatters import (
    format_as_json,
    format_as_markdown,
    format_entry_detail,
    format_search_results,
)
from src.retrieval.result import SearchResult
from src.storage.markdown_store import Entry


@dataclass
class DummyPayload:
    """Simple dataclass payload for JSON serialization tests."""

    name: str
    path: Path


@pytest.fixture
def sample_entry() -> Entry:
    """Create a sample Entry with metadata for formatter tests."""
    entry = Entry(
        title="测试标题",
        source_type="wechat",
        source_url="https://example.com/article",
        archived_at="2026-02-15 10:00:00",
        tags=[],
        keywords=["关键词A", "关键词B"],
        abstract="",
        summary_one_sentence="一句话摘要",
        summary_100_words="这是100字摘要",
        search_strategy="keyword",
        word_count=321,
        related_docs=["doc1", "doc2"],
        reading_status="未读",
        rating=4,
        notes="读书笔记内容",
        content="正文内容",
    )
    entry.metadata = {
        "author": "Alice",
        "published_time": "2026-02-14",
        "tags": ["meta1", "meta2"],
        "description": "元数据摘要",
    }
    entry.knowledge_id = 42
    return entry


@pytest.fixture
def search_results():
    """Create a list of mixed search results for table formatting."""
    long_title = "L" * 35
    result_one = SearchResult(
        knowledge_id=101,
        title=long_title,
        score=0.9123,
        highlight="片段",
        metadata={"tags": ["ai", "test"]},
    )
    result_two = {
        "entry_id": 202,
        "title": "短标题",
        "score": None,
        "tags": "tag1, tag2",
    }
    return long_title, [result_one, result_two]


def test_format_as_json(sample_entry):
    """JSON 格式化应支持 dataclass、Path 和额外属性。"""
    payload = {
        "entry": sample_entry,
        "dummy": DummyPayload(name="测试", path=Path("data/vault/test.md")),
    }

    json_text = format_as_json(payload)
    parsed = json.loads(json_text)

    assert parsed["dummy"]["path"] == str(Path("data/vault/test.md"))
    assert parsed["dummy"]["name"] == "测试"
    assert parsed["entry"]["metadata"]["author"] == "Alice"
    assert "测试标题" in json_text


def test_format_as_markdown(sample_entry):
    """Markdown 格式化应包含标题、作者、时间、标签和摘要。"""
    markdown = format_as_markdown(sample_entry)

    assert markdown.startswith("# 测试标题")
    assert "**作者**: Alice" in markdown
    assert "**时间**: 2026-02-14" in markdown
    assert "**标签**: meta1, meta2" in markdown
    assert "## 摘要" in markdown
    assert "元数据摘要" in markdown
    assert markdown.endswith("\n")


def test_format_search_results(search_results):
    """搜索结果应输出正确列、截断标题和格式化分数。"""
    long_title, results = search_results
    table = format_search_results(results)

    assert table.title == "搜索结果"
    assert [column.header for column in table.columns] == ["ID", "标题", "得分", "标签"]

    expected_title = long_title[:27] + "..."
    assert table.columns[0]._cells == ["101", "202"]
    assert table.columns[1]._cells == [expected_title, "短标题"]
    assert table.columns[2]._cells == ["0.91", ""]
    assert table.columns[3]._cells == ["ai, test", "tag1, tag2"]


def test_format_entry_detail(sample_entry):
    """条目详情面板应包含关键字段与格式化后的内容。"""
    panel = format_entry_detail(sample_entry)

    assert panel.title == "条目详情 #42"
    content = panel.renderable

    assert "标题: 测试标题" in content
    assert "作者: Alice" in content
    assert "发布时间: 2026-02-14" in content
    assert "来源类型: wechat" in content
    assert "来源 URL: https://example.com/article" in content
    assert "标签: meta1, meta2" in content
    assert "关键词: 关键词A, 关键词B" in content
    assert "摘要:" in content
    assert "  元数据摘要" in content
    assert "一句话摘要: 一句话摘要" in content
    assert "100字摘要:" in content
    assert "  这是100字摘要" in content
    assert "检索策略: keyword" in content
    assert "字数: 321" in content
    assert "阅读状态: 未读" in content
    assert "评分: 4" in content
    assert "笔记:" in content
    assert "  读书笔记内容" in content
