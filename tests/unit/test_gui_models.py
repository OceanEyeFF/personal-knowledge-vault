"""GUI 数据模型单元测试。

本文件仅测试不依赖 PySide6 的纯数据逻辑部分（工具函数、分页计算、
数据格式化等）。PySide6 控件的交互测试需要 pytest-qt 环境，
将在 M13 E2E 测试中处理。

测试覆盖：
1. parse_tags_string — 标签字符串解析的各种边界情况
2. serialize_entry_summary — 条目摘要序列化字段完整性
3. clamp_param — 参数范围限制
4. pagination_calc — 分页计算逻辑
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# 纯数据工具函数（不依赖 Qt，从 entry_model 逻辑抽出进行测试）
# ============================================================


def parse_tags_string(tags_raw: Any) -> list[str]:
    """解析标签字段为字符串列表。

    支持 str（逗号分隔）和 list 两种输入格式。
    过滤空白标签。

    Args:
        tags_raw: 标签原始值（str 或 list）。

    Returns:
        清理后的标签字符串列表。
    """
    if not tags_raw:
        return []
    if isinstance(tags_raw, list):
        return [t.strip() for t in tags_raw if t and t.strip()]
    if isinstance(tags_raw, str):
        return [t.strip() for t in tags_raw.split(",") if t.strip()]
    return []


def format_tags_display(tags_raw: Any, max_count: int = 3) -> str:
    """将标签字段格式化为最多 max_count 个标签的显示字符串。

    超出部分显示 "+N" 后缀。标签之间用空格连接。

    Args:
        tags_raw: 标签原始值（str 或 list）。
        max_count: 最多显示的标签数量，默认 3。

    Returns:
        格式化后的标签显示字符串。
    """
    tag_list = parse_tags_string(tags_raw)
    if not tag_list:
        return ""

    display_tags = tag_list[:max_count]
    remaining = len(tag_list) - len(display_tags)
    result = " ".join(display_tags)
    if remaining > 0:
        result += f" +{remaining}"
    return result


def serialize_entry_summary(entry: dict) -> dict:
    """从完整条目字典提取摘要字段，用于 GUI 展示。

    Args:
        entry: 原始条目字典（来自 SQLiteStore.list_entries()）。

    Returns:
        包含 knowledge_id、title、source_type、tags、
        word_count、archived_at 字段的摘要字典。
    """
    return {
        "knowledge_id": entry.get("knowledge_id", ""),
        "title": entry.get("title", ""),
        "source_type": entry.get("source_type", ""),
        "tags": entry.get("tags", ""),
        "word_count": entry.get("word_count", 0),
        "archived_at": entry.get("archived_at", ""),
    }


def clamp_param(value: int, min_val: int, max_val: int) -> int:
    """将数值限制在 [min_val, max_val] 范围内。

    Args:
        value: 输入值。
        min_val: 最小允许值。
        max_val: 最大允许值。

    Returns:
        限制后的数值。
    """
    return max(min_val, min(value, max_val))


def calc_total_pages(total: int, page_size: int) -> int:
    """计算总页数。

    Args:
        total: 总条目数。
        page_size: 每页大小。

    Returns:
        总页数（最小为 1）。

    Raises:
        ValueError: page_size <= 0 时。
    """
    if page_size <= 0:
        raise ValueError(f"page_size 必须大于 0，当前: {page_size}")
    if total <= 0:
        return 1
    return math.ceil(total / page_size)


# ============================================================
# 测试：parse_tags_string 标签解析
# ============================================================


class TestParseTagsString:
    """测试标签字符串解析的边界情况。"""

    def test_empty_string(self) -> None:
        """空字符串应返回空列表。"""
        assert parse_tags_string("") == []

    def test_none_value(self) -> None:
        """None 值应返回空列表。"""
        assert parse_tags_string(None) == []

    def test_single_tag(self) -> None:
        """单个标签应返回单元素列表。"""
        assert parse_tags_string("AI") == ["AI"]

    def test_multiple_tags_comma_separated(self) -> None:
        """逗号分隔的多标签应正确解析。"""
        assert parse_tags_string("AI,Python,机器学习") == ["AI", "Python", "机器学习"]

    def test_tags_with_spaces_around_commas(self) -> None:
        """逗号周围有空格的标签应正确去除空格。"""
        result = parse_tags_string("AI , Python , 机器学习")
        assert result == ["AI", "Python", "机器学习"]

    def test_tags_with_leading_trailing_spaces(self) -> None:
        """标签前后有空格应正确去除。"""
        assert parse_tags_string("  AI  ,  Python  ") == ["AI", "Python"]

    def test_empty_tags_in_middle(self) -> None:
        """逗号之间的空标签应被过滤。"""
        result = parse_tags_string("AI,,Python")
        assert result == ["AI", "Python"]

    def test_only_commas(self) -> None:
        """只有逗号时应返回空列表。"""
        assert parse_tags_string(",,,") == []

    def test_list_input(self) -> None:
        """list 输入应原样返回（去除空白）。"""
        assert parse_tags_string(["AI", "Python", "机器学习"]) == ["AI", "Python", "机器学习"]

    def test_list_with_empty_strings(self) -> None:
        """list 中的空字符串应被过滤。"""
        result = parse_tags_string(["AI", "", "  ", "Python"])
        assert result == ["AI", "Python"]

    def test_list_with_spaces(self) -> None:
        """list 中的标签应去除前后空格。"""
        assert parse_tags_string(["  AI  ", " Python "]) == ["AI", "Python"]

    def test_invalid_type_returns_empty(self) -> None:
        """不支持的类型（如 int）应返回空列表。"""
        assert parse_tags_string(123) == []

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("", []),
            ("A", ["A"]),
            ("A,B,C", ["A", "B", "C"]),
            ("A, B, C", ["A", "B", "C"]),
            ("A,,B", ["A", "B"]),
            (["X", "Y"], ["X", "Y"]),
            (None, []),
        ],
    )
    def test_parametrized_cases(self, raw: Any, expected: list[str]) -> None:
        """参数化测试多种边界情况。"""
        assert parse_tags_string(raw) == expected


# ============================================================
# 测试：format_tags_display 标签格式化显示
# ============================================================


class TestFormatTagsDisplay:
    """测试标签显示字符串格式化。"""

    def test_empty_returns_empty_string(self) -> None:
        """空标签应返回空字符串。"""
        assert format_tags_display("") == ""

    def test_single_tag_display(self) -> None:
        """单个标签应直接显示。"""
        assert format_tags_display("AI") == "AI"

    def test_three_tags_display(self) -> None:
        """恰好 3 个标签应全部显示，无 "+N"。"""
        assert format_tags_display("A,B,C") == "A B C"

    def test_more_than_three_shows_suffix(self) -> None:
        """超过 3 个标签应显示 "+N" 后缀。"""
        result = format_tags_display("A,B,C,D,E")
        assert result == "A B C +2"

    def test_custom_max_count(self) -> None:
        """自定义 max_count=2 时应限制为 2 个。"""
        result = format_tags_display("A,B,C,D", max_count=2)
        assert result == "A B +2"

    def test_exactly_max_count(self) -> None:
        """恰好等于 max_count 时不显示 "+N"。"""
        result = format_tags_display("X,Y,Z", max_count=3)
        assert result == "X Y Z"

    @pytest.mark.parametrize(
        "raw,max_count,expected",
        [
            ("", 3, ""),
            ("A", 3, "A"),
            ("A,B,C", 3, "A B C"),
            ("A,B,C,D", 3, "A B C +1"),
            ("A,B,C,D,E,F", 3, "A B C +3"),
            ("A,B", 1, "A +1"),
        ],
    )
    def test_parametrized(self, raw: str, max_count: int, expected: str) -> None:
        """参数化测试格式化显示逻辑。"""
        assert format_tags_display(raw, max_count) == expected


# ============================================================
# 测试：serialize_entry_summary 条目摘要序列化
# ============================================================


class TestSerializeEntrySummary:
    """测试条目摘要字典的字段完整性。"""

    _REQUIRED_FIELDS = [
        "knowledge_id",
        "title",
        "source_type",
        "tags",
        "word_count",
        "archived_at",
    ]

    def _make_entry(self, **overrides: Any) -> dict:
        """构造标准测试条目字典。"""
        base = {
            "knowledge_id": 42,
            "title": "测试文章标题",
            "source_type": "wechat",
            "tags": "AI,Python",
            "keywords": "人工智能,语言模型",
            "content": "这是正文内容",
            "summary_one_sentence": "一句话摘要",
            "summary_100_words": "100字摘要",
            "word_count": 500,
            "archived_at": "2026-02-19 10:00:00",
            "file_path": ".data/vault/wechat/test.md",
        }
        base.update(overrides)
        return base

    def test_all_required_fields_present(self) -> None:
        """序列化结果应包含所有必需字段。"""
        entry = self._make_entry()
        summary = serialize_entry_summary(entry)
        for field in self._REQUIRED_FIELDS:
            assert field in summary, f"缺少字段: {field}"

    def test_correct_field_values(self) -> None:
        """序列化结果的字段值应与源数据一致。"""
        entry = self._make_entry()
        summary = serialize_entry_summary(entry)
        assert summary["knowledge_id"] == 42
        assert summary["title"] == "测试文章标题"
        assert summary["source_type"] == "wechat"
        assert summary["tags"] == "AI,Python"
        assert summary["word_count"] == 500
        assert summary["archived_at"] == "2026-02-19 10:00:00"

    def test_private_fields_not_included(self) -> None:
        """content、keywords 等不应出现在摘要中。"""
        entry = self._make_entry()
        summary = serialize_entry_summary(entry)
        assert "content" not in summary
        assert "summary_one_sentence" not in summary
        assert "file_path" not in summary

    def test_empty_entry_uses_defaults(self) -> None:
        """空字典输入应使用合理默认值。"""
        summary = serialize_entry_summary({})
        assert summary["knowledge_id"] == ""
        assert summary["title"] == ""
        assert summary["word_count"] == 0
        assert summary["tags"] == ""

    def test_partial_entry(self) -> None:
        """部分字段缺失时应用默认值。"""
        entry = {"knowledge_id": 1, "title": "仅有标题"}
        summary = serialize_entry_summary(entry)
        assert summary["knowledge_id"] == 1
        assert summary["title"] == "仅有标题"
        assert summary["source_type"] == ""
        assert summary["word_count"] == 0


# ============================================================
# 测试：clamp_param 参数范围限制
# ============================================================


class TestClampParam:
    """测试参数范围夹取函数。"""

    @pytest.mark.parametrize(
        "value,min_val,max_val,expected",
        [
            (5, 0, 10, 5),      # 在范围内，原值返回
            (-1, 0, 10, 0),     # 低于最小值，返回 min
            (11, 0, 10, 10),    # 超过最大值，返回 max
            (0, 0, 10, 0),      # 等于最小值
            (10, 0, 10, 10),    # 等于最大值
            (0, 0, 0, 0),       # min == max 时
            (100, 20, 50, 50),  # 远超最大值
            (-100, 0, 20, 0),   # 远低于最小值
        ],
    )
    def test_clamp_cases(
        self,
        value: int,
        min_val: int,
        max_val: int,
        expected: int,
    ) -> None:
        """参数化测试各种边界情况。"""
        assert clamp_param(value, min_val, max_val) == expected


# ============================================================
# 测试：calc_total_pages 分页计算
# ============================================================


class TestCalcTotalPages:
    """测试分页总数计算函数。"""

    @pytest.mark.parametrize(
        "total,page_size,expected",
        [
            (0, 20, 1),       # 无条目时最少 1 页
            (1, 20, 1),       # 1 条记录，不足 1 页
            (20, 20, 1),      # 恰好 1 页
            (21, 20, 2),      # 比 1 页多 1 条
            (40, 20, 2),      # 恰好 2 页
            (41, 20, 3),      # 比 2 页多 1 条
            (100, 20, 5),     # 整除情况
            (101, 20, 6),     # 向上取整
            (1, 1, 1),        # page_size=1
            (5, 1, 5),        # page_size=1，多条
            (203, 20, 11),    # MCP 测试用例数 (203 tests)
        ],
    )
    def test_page_count(self, total: int, page_size: int, expected: int) -> None:
        """参数化测试分页计算正确性。"""
        assert calc_total_pages(total, page_size) == expected

    def test_invalid_page_size_raises(self) -> None:
        """page_size <= 0 时应抛出 ValueError。"""
        with pytest.raises(ValueError, match="page_size"):
            calc_total_pages(10, 0)

    def test_negative_page_size_raises(self) -> None:
        """负数 page_size 应抛出 ValueError。"""
        with pytest.raises(ValueError):
            calc_total_pages(10, -1)

    def test_negative_total_treated_as_zero(self) -> None:
        """负数 total 应视为无条目，返回 1 页。"""
        assert calc_total_pages(-5, 20) == 1

    def test_large_dataset(self) -> None:
        """大数据集的分页计算应正确。"""
        # 10000 条记录，每页 20 条，= 500 页
        assert calc_total_pages(10000, 20) == 500
        # 10001 条记录，每页 20 条，= 501 页
        assert calc_total_pages(10001, 20) == 501
