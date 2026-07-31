"""EntryTableModel 的可观察 Qt 模型合同测试。

测试仅通过 QAbstractTableModel 的公开接口和信号驱动生产模型，不在测试内
复制标签解析、格式化、序列化、参数夹取或分页实现。
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import QModelIndex, Qt

from src.gui.models.entry_model import EntryTableModel


Entry = dict[str, Any]


@pytest.fixture
def entry() -> Entry:
    """返回覆盖全部展示列的代表性条目。"""
    return {
        "knowledge_id": 42,
        "title": "测试文章标题",
        "source_type": "wechat",
        "tags": "AI, Python,机器学习,Qt",
        "word_count": 500,
        "archived_at": "2026-02-19 10:00:00",
    }


@pytest.fixture
def model(entry: Entry) -> EntryTableModel:
    """返回包含一行数据的生产表格模型。"""
    return EntryTableModel([entry])


def _display(model: EntryTableModel, column: int, row: int = 0) -> Any:
    """读取一个单元格的 DisplayRole，保持断言聚焦于公开 Qt 接口。"""
    return model.data(model.index(row, column), Qt.DisplayRole)


class TestTableShapeAndDisplay:
    """验证 QAbstractTableModel 的行列和展示角色合同。"""

    def test_empty_and_populated_models_report_table_shape(
        self,
        model: EntryTableModel,
    ) -> None:
        empty_model = EntryTableModel([])

        assert empty_model.rowCount() == 0
        assert empty_model.columnCount() == 6
        assert model.rowCount() == 1
        assert model.columnCount() == 6

    def test_valid_parent_has_no_child_rows_or_columns(
        self,
        model: EntryTableModel,
    ) -> None:
        parent = model.index(0, EntryTableModel.COL_ID)

        assert parent.isValid()
        assert model.rowCount(parent) == 0
        assert model.columnCount(parent) == 0

    def test_display_role_maps_all_six_entry_fields(
        self,
        model: EntryTableModel,
    ) -> None:
        displayed_row = [
            _display(model, column)
            for column in range(model.columnCount())
        ]

        assert displayed_row == [
            "42",
            "测试文章标题",
            "wechat",
            "AI Python 机器学习 +1",
            "500",
            "2026-02-19",
        ]

    def test_invalid_index_and_unsupported_role_have_no_data(
        self,
        model: EntryTableModel,
    ) -> None:
        valid_index = model.index(0, EntryTableModel.COL_TITLE)

        assert model.data(QModelIndex(), Qt.DisplayRole) is None
        assert model.data(valid_index, Qt.ToolTipRole) is None


class TestDisplayFormatting:
    """验证展示文本的边界，而不直接调用模型私有辅助方法。"""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            pytest.param("A" * 60, "A" * 60, id="exactly-60-characters"),
            pytest.param(
                "A" * 61,
                ("A" * 60) + "...",
                id="truncate-after-60-characters",
            ),
        ],
    )
    def test_title_truncation_boundary(self, title: str, expected: str) -> None:
        title_model = EntryTableModel([{"title": title}])

        assert _display(title_model, EntryTableModel.COL_TITLE) == expected

    @pytest.mark.parametrize(
        ("tags", "expected"),
        [
            pytest.param("", "", id="empty-string"),
            pytest.param(None, "", id="none"),
            pytest.param(
                "AI, Python,,机器学习",
                "AI Python 机器学习",
                id="comma-separated-and-empty-items",
            ),
            pytest.param(
                "A,B,C,D,E",
                "A B C +2",
                id="string-limited-to-three",
            ),
            pytest.param(
                [" AI ", "", "Python", "  ", "Qt", "GUI"],
                "AI Python Qt +1",
                id="list-trimmed-filtered-and-limited",
            ),
            pytest.param(123, "", id="unsupported-type"),
        ],
    )
    def test_tags_are_formatted_through_display_role(
        self,
        tags: Any,
        expected: str,
    ) -> None:
        tags_model = EntryTableModel([{"tags": tags}])

        assert _display(tags_model, EntryTableModel.COL_TAGS) == expected

    @pytest.mark.parametrize(
        ("entry_fields", "expected"),
        [
            pytest.param({}, "0", id="missing"),
            pytest.param({"word_count": 0}, "0", id="zero"),
            pytest.param({"word_count": 4200}, "4200", id="positive"),
            pytest.param({"word_count": None}, "0", id="none"),
        ],
    )
    def test_word_count_is_rendered_as_text(
        self,
        entry_fields: Entry,
        expected: str,
    ) -> None:
        word_count_model = EntryTableModel([entry_fields])

        assert (
            _display(word_count_model, EntryTableModel.COL_WORD_COUNT)
            == expected
        )

    @pytest.mark.parametrize(
        ("archived_at", "expected"),
        [
            pytest.param(
                "2026-02-19 10:00:00",
                "2026-02-19",
                id="timestamp",
            ),
            pytest.param("2026-02-19", "2026-02-19", id="date-only"),
            pytest.param("2026-02", "2026-02", id="short-value"),
            pytest.param("", "", id="empty-string"),
            pytest.param(None, "", id="none"),
        ],
    )
    def test_archived_at_displays_date_portion(
        self,
        archived_at: Any,
        expected: str,
    ) -> None:
        date_model = EntryTableModel([{"archived_at": archived_at}])

        assert _display(date_model, EntryTableModel.COL_ARCHIVED_AT) == expected


class TestHeadersAndAlignment:
    """验证表头文本、行号和单元格对齐合同。"""

    def test_horizontal_headers_match_public_column_order(
        self,
        model: EntryTableModel,
    ) -> None:
        headers = [
            model.headerData(section, Qt.Horizontal, Qt.DisplayRole)
            for section in range(model.columnCount())
        ]

        assert headers == ["ID", "标题", "来源", "标签", "字数", "归档时间"]
        assert (
            model.headerData(-1, Qt.Horizontal, Qt.DisplayRole)
            is None
        )
        assert (
            model.headerData(6, Qt.Horizontal, Qt.DisplayRole)
            is None
        )
        assert (
            model.headerData(0, Qt.Horizontal, Qt.ToolTipRole)
            is None
        )

    def test_vertical_headers_are_one_based_row_numbers(self) -> None:
        two_row_model = EntryTableModel([{}, {}])

        headers = [
            two_row_model.headerData(row, Qt.Vertical, Qt.DisplayRole)
            for row in range(two_row_model.rowCount())
        ]

        assert headers == ["1", "2"]

    def test_word_count_is_right_aligned_and_other_columns_are_left_aligned(
        self,
        model: EntryTableModel,
    ) -> None:
        for column in range(model.columnCount()):
            index = model.index(0, column)
            alignment = model.data(index, Qt.TextAlignmentRole)
            if column == EntryTableModel.COL_WORD_COUNT:
                assert alignment == Qt.AlignRight | Qt.AlignVCenter
            else:
                assert alignment == Qt.AlignLeft | Qt.AlignVCenter


class TestEntryAccessAndReset:
    """验证原始条目访问和整表更新通知合同。"""

    def test_get_entry_returns_original_row_object(
        self,
        model: EntryTableModel,
        entry: Entry,
    ) -> None:
        assert model.get_entry(0) is entry

    @pytest.mark.parametrize("row", [-1, 1, 999])
    def test_get_entry_returns_none_out_of_bounds(
        self,
        model: EntryTableModel,
        row: int,
    ) -> None:
        assert model.get_entry(row) is None

    def test_update_entries_replaces_rows_and_emits_model_reset(
        self,
        model: EntryTableModel,
        qtbot: Any,
    ) -> None:
        replacement = [
            {
                "knowledge_id": 7,
                "title": "替换后的第一条",
                "word_count": 70,
            },
            {
                "knowledge_id": 8,
                "title": "替换后的第二条",
                "word_count": 80,
            },
        ]

        with qtbot.waitSignal(model.modelReset, timeout=1000):
            model.update_entries(replacement)

        assert model.rowCount() == 2
        assert model.get_entry(0) is replacement[0]
        assert model.get_entry(1) is replacement[1]
        assert (
            _display(model, EntryTableModel.COL_TITLE, row=1)
            == "替换后的第二条"
        )
