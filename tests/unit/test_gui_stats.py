"""StatsView pytest-qt 单元测试。

覆盖 M11 验收标准:
- StatsView UI 结构（概况标签、来源分布、热门标签、刷新按钮）
- 数据加载与展示
- 空数据与错误处理
- 刷新功能

测试策略：Mock SQLiteStore.get_statistics()，验证 UI 组件渲染。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch, MagicMock

import pytest
from PySide6.QtWidgets import QPushButton

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Mock 数据
# ============================================================

MOCK_STATS = {
    "total_entries": 10,
    "by_source_type": [("wechat", 5), ("zhihu", 3), ("text", 2)],
    "top_tags": [{"name": "AI", "count": 5}, {"name": "Python", "count": 3}],
}

MOCK_STATS_EMPTY = {
    "total_entries": 0,
    "by_source_type": [],
    "top_tags": [],
}


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_store():
    """创建 Mock SQLiteStore。"""
    store = MagicMock()
    store.get_statistics.return_value = MOCK_STATS
    store.get_all_tags_with_count.return_value = []
    store.list_entries.return_value = []
    store.count_entries.return_value = 0
    return store


@pytest.fixture
def stats_view(qtbot, mock_store):
    """创建带有 Mock 数据的 StatsView。

    使用 yield 确保 mock 上下文在整个测试期间保持活跃。
    """
    with patch("src.gui.stores.get_sqlite_store", return_value=mock_store), \
         patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
        from src.gui.views.stats_view import StatsView
        view = StatsView()
        qtbot.addWidget(view)
        yield view


@pytest.fixture
def empty_stats_view(qtbot):
    """创建空数据的 StatsView。"""
    empty_store = MagicMock()
    empty_store.get_statistics.return_value = MOCK_STATS_EMPTY
    empty_store.get_all_tags_with_count.return_value = []
    empty_store.list_entries.return_value = []
    empty_store.count_entries.return_value = 0

    with patch("src.gui.stores.get_sqlite_store", return_value=empty_store), \
         patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
        from src.gui.views.stats_view import StatsView
        view = StatsView()
        qtbot.addWidget(view)
        yield view


@pytest.fixture
def error_stats_view(qtbot):
    """创建加载失败的 StatsView。"""
    err_store = MagicMock()
    err_store.get_statistics.side_effect = Exception("DB 连接失败")
    err_store.get_all_tags_with_count.return_value = []
    err_store.list_entries.return_value = []
    err_store.count_entries.return_value = 0

    with patch("src.gui.stores.get_sqlite_store", return_value=err_store), \
         patch("src.gui.stores.get_bm25_retriever", return_value=MagicMock()), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
        from src.gui.views.stats_view import StatsView
        view = StatsView()
        qtbot.addWidget(view)
        yield view


# ============================================================
# UI 结构验证
# ============================================================

class TestStatsViewStructure:
    """验证 StatsView UI 结构。"""

    def test_view_is_created(self, stats_view):
        """视图实例化不崩溃。"""
        assert stats_view is not None

    def test_has_refresh_button(self, stats_view):
        """包含刷新按钮。"""
        assert stats_view._refresh_btn is not None
        assert isinstance(stats_view._refresh_btn, QPushButton)

    def test_has_overview_labels(self, stats_view):
        """包含概况标签（总条目数、来源类型数、标签总数）。"""
        assert stats_view._total_label is not None
        assert stats_view._source_type_count_label is not None
        assert stats_view._tag_count_label is not None

    def test_has_source_group(self, stats_view):
        """包含来源分布分组。"""
        assert stats_view._source_group is not None

    def test_has_tags_group(self, stats_view):
        """包含热门标签分组。"""
        assert stats_view._tags_group is not None

    def test_has_overview_group(self, stats_view):
        """包含概况分组。"""
        assert stats_view._overview_group is not None


# ============================================================
# 数据展示验证
# ============================================================

class TestStatsViewData:
    """验证 StatsView 数据展示。"""

    def test_displays_total_count(self, stats_view):
        """总条目数正确显示。"""
        label_text = stats_view._total_label.text()
        assert "10" in label_text

    def test_displays_source_type_count(self, stats_view):
        """来源类型数正确显示。"""
        label_text = stats_view._source_type_count_label.text()
        assert "3" in label_text  # wechat, zhihu, text

    def test_displays_tag_count(self, stats_view):
        """标签总数正确显示。"""
        label_text = stats_view._tag_count_label.text()
        assert "2" in label_text  # AI, Python

    def test_displays_source_distribution(self, stats_view):
        """来源分布分组包含进度条行（非"暂无数据"）。"""
        # 来源分布有 3 项，每项一个 row_widget
        layout = stats_view._source_layout
        assert layout.count() == 3

    def test_displays_tag_distribution(self, stats_view):
        """热门标签分组包含进度条行。"""
        layout = stats_view._tags_layout
        assert layout.count() == 2  # AI, Python

    def test_handles_empty_data(self, empty_stats_view):
        """空数据时显示"暂无数据"占位符。"""
        label_text = empty_stats_view._total_label.text()
        assert "0" in label_text

        # 来源分布和标签分组应显示"暂无数据"
        source_layout = empty_stats_view._source_layout
        assert source_layout.count() == 1  # 仅一个占位 QLabel

    def test_handles_error_data(self, error_stats_view):
        """加载失败时显示错误提示。"""
        label_text = error_stats_view._total_label.text()
        assert "失败" in label_text

    @pytest.mark.parametrize(
        "malformed_stats",
        [
            pytest.param("not-a-mapping", id="string-root"),
            pytest.param((0, [], []), id="tuple-root"),
            pytest.param(
                MappingProxyType(MOCK_STATS),
                id="frozen-root-mapping",
            ),
            pytest.param({}, id="missing-fields"),
            pytest.param(
                {
                    "total_entries": True,
                    "by_source_type": [],
                    "top_tags": [],
                },
                id="bool-total",
            ),
            pytest.param(
                {
                    "total_entries": -1,
                    "by_source_type": [],
                    "top_tags": [],
                },
                id="negative-total",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": (("text", 1),),
                    "top_tags": [],
                },
                id="frozen-source-sequence",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [["text", 1]],
                    "top_tags": [],
                },
                id="source-item-not-tuple",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [("", 1)],
                    "top_tags": [],
                },
                id="empty-source-name",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [("text", True)],
                    "top_tags": [],
                },
                id="bool-source-count",
            ),
            pytest.param(
                {
                    "total_entries": 2,
                    "by_source_type": [("text", 1), ("text", 1)],
                    "top_tags": [],
                },
                id="duplicate-source-name",
            ),
            pytest.param(
                {
                    "total_entries": 2,
                    "by_source_type": [("text", 1)],
                    "top_tags": [],
                },
                id="source-count-sum-mismatch",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [],
                    "top_tags": ({"name": "tag", "count": 1},),
                },
                id="frozen-tag-sequence",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [],
                    "top_tags": [
                        MappingProxyType({
                            "name": "stats-secret\r\napi_key=x",
                            "count": 1,
                        })
                    ],
                },
                id="frozen-tag-mapping",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [],
                    "top_tags": [{"name": "tag"}],
                },
                id="missing-tag-count",
            ),
            pytest.param(
                {
                    "total_entries": 1,
                    "by_source_type": [],
                    "top_tags": [{"name": "tag", "count": True}],
                },
                id="bool-tag-count",
            ),
        ],
    )
    def test_malformed_projection_renders_fixed_error_without_raw_values(
        self,
        stats_view,
        mock_store,
        malformed_stats,
        caplog,
    ):
        """Corrupt storage projections fail closed before reaching widgets."""
        mock_store.get_statistics.return_value = malformed_stats

        with caplog.at_level("WARNING", logger="pkv.gui.stats"):
            stats_view.refresh()

        assert stats_view._stats is None
        assert stats_view._total_label.text() == "总条目数: 加载失败"
        assert stats_view._source_layout.count() == 1
        assert stats_view._source_layout.itemAt(0).widget().text() == "加载失败"
        assert stats_view._tags_layout.count() == 1
        assert stats_view._tags_layout.itemAt(0).widget().text() == "加载失败"
        visible = "\n".join(
            (
                stats_view._total_label.text(),
                stats_view._source_type_count_label.text(),
                stats_view._tag_count_label.text(),
                stats_view._source_layout.itemAt(0).widget().text(),
                stats_view._tags_layout.itemAt(0).widget().text(),
            )
        )
        assert "stats-secret" not in visible
        assert "stats-secret" not in caplog.text

    def test_refresh_reloads_data(self, stats_view, mock_store):
        """刷新按钮触发数据重新加载。"""
        initial_call_count = mock_store.get_statistics.call_count
        stats_view.refresh()
        assert mock_store.get_statistics.call_count == initial_call_count + 1

    def test_refresh_button_triggers_refresh(self, stats_view, mock_store):
        """点击刷新按钮触发刷新。"""
        initial_call_count = mock_store.get_statistics.call_count
        stats_view._refresh_btn.click()
        assert mock_store.get_statistics.call_count == initial_call_count + 1

    def test_refresh_updates_labels(self, stats_view, mock_store):
        """刷新后标签内容更新。"""
        # 修改 mock 返回新数据
        new_stats = {
            "total_entries": 99,
            "by_source_type": [("wechat", 99)],
            "top_tags": [{"name": "NewTag", "count": 10}],
        }
        mock_store.get_statistics.return_value = new_stats
        stats_view.refresh()

        assert "99" in stats_view._total_label.text()
        assert "1" in stats_view._source_type_count_label.text()
