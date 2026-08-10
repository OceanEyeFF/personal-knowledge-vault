"""BrowserView pytest-qt 单元测试。

覆盖 M10 验收标准:
2. 标签树正确显示 → 点击标签筛选列表 → 点击条目预览 Markdown

测试策略：Mock 存储层，验证 UI 组件交互逻辑。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch, MagicMock

import pytest
from PySide6.QtCore import Qt

from src.runtime.errors import OperationStatus, StorageStage
from src.storage.coordinator import StorageOperationResult

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Mock 数据
# ============================================================

MOCK_TAGS = [
    {"name": "AI", "count": 5},
    {"name": "Python", "count": 3},
    {"name": "知识管理", "count": 2},
]

MOCK_ENTRIES = [
    {
        "knowledge_id": 1,
        "title": "深度学习入门指南",
        "source_type": "wechat",
        "tags": "AI,深度学习",
        "word_count": 3500,
        "archived_at": "2026-02-15 10:30:00",
        "file_path": "wechat/2026/02/20260215-dl-guide.md",
        "summary_one_sentence": "一篇关于深度学习基础概念的入门文章",
    },
    {
        "knowledge_id": 2,
        "title": "Python 最佳实践",
        "source_type": "zhihu",
        "tags": "Python,编程",
        "word_count": 2100,
        "archived_at": "2026-02-16 14:20:00",
        "file_path": "zhihu/2026/02/20260216-python-best.md",
        "summary_one_sentence": "Python 编程最佳实践总结",
    },
]


def _entry_page(count: int, *, start_id: int = 1) -> list[dict]:
    """Build one exact, identity-unique synthetic storage page."""

    return [
        {
            **MOCK_ENTRIES[index % len(MOCK_ENTRIES)],
            "knowledge_id": start_id + index,
        }
        for index in range(count)
    ]


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_store():
    """创建 Mock SQLiteStore。"""
    store = MagicMock()
    store.get_all_tags_with_count.return_value = MOCK_TAGS
    store.list_entries.return_value = MOCK_ENTRIES
    store.count_entries.return_value = len(MOCK_ENTRIES)
    return store


@pytest.fixture
def browser_view(qtbot, mock_store):
    """创建带有 Mock 数据的 BrowserView。

    使用 yield 确保 mock 上下文在整个测试期间保持活跃，
    因为 BrowserView 的方法内部使用延迟导入（from src.gui.stores import xxx）。
    """
    with patch("src.gui.stores.get_sqlite_store", return_value=mock_store), \
         patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
        from src.gui.views.browser_view import BrowserView
        view = BrowserView()
        qtbot.addWidget(view)
        yield view


# ============================================================
# UI 结构验证
# ============================================================

class TestBrowserViewStructure:
    """测试 BrowserView 的 UI 结构。"""

    def test_view_is_created(self, browser_view):
        """视图实例化不崩溃。"""
        assert browser_view is not None

    def test_has_tag_tree_view(self, browser_view):
        """包含标签树视图。"""
        assert browser_view._tag_view is not None

    def test_has_entry_table_view(self, browser_view):
        """包含条目表格视图。"""
        assert browser_view._entry_view is not None

    def test_has_preview_text(self, browser_view):
        """包含预览文本框。"""
        assert browser_view._preview_text is not None
        assert browser_view._preview_text.isReadOnly()

    def test_has_pagination_buttons(self, browser_view):
        """包含分页按钮。"""
        assert browser_view._prev_btn is not None
        assert browser_view._next_btn is not None
        assert browser_view._page_label is not None


# ============================================================
# 验收标准 2: 标签树正确显示
# ============================================================

class TestTagTree:
    """测试标签树加载和交互。"""

    def test_tags_loaded_on_init(self, browser_view, mock_store):
        """初始化时加载标签。"""
        mock_store.get_all_tags_with_count.assert_called()

    def test_tag_model_has_correct_count(self, browser_view):
        """标签模型包含正确的标签数量（含"全部"根节点）。"""
        model = browser_view._tag_model
        # TagTreeModel 会先插入"全部"根节点，再添加各标签
        assert model.rowCount() == len(MOCK_TAGS) + 1

    def test_tag_click_filters_entries(self, browser_view, mock_store):
        """点击标签触发带 tag 参数的加载。"""
        # 索引 0 是"全部"根节点，索引 1 是第一个真实标签
        first_tag_index = browser_view._tag_model.index(1, 0)
        browser_view.on_tag_selected(first_tag_index)

        # 验证 list_entries 被调用时带了 tag 参数
        calls = mock_store.list_entries.call_args_list
        assert len(calls) >= 2  # 初始加载 + 标签点击
        last_call = calls[-1]
        assert last_call.kwargs.get("tag") is not None

    def test_tag_click_clears_preview(self, browser_view):
        """点击标签后清空预览区域。"""
        browser_view._preview_text.setPlainText("旧内容")
        # 索引 1 是第一个真实标签（索引 0 是"全部"）
        first_tag_index = browser_view._tag_model.index(1, 0)
        browser_view.on_tag_selected(first_tag_index)
        assert browser_view._preview_text.toPlainText() == ""

    def test_tag_db_failure_is_explicit_and_redacted(
        self, browser_view, mock_store, caplog
    ):
        """标签查询失败不能伪装为空标签，也不能泄漏底层异常。"""
        canary = (
            r"C:\private\vault.db"
            "\r\nAuthorization: Bearer TAG-SECRET-CANARY"
        )
        previous_rows = browser_view._tag_model.rowCount()
        mock_store.get_all_tags_with_count.side_effect = RuntimeError(canary)
        caplog.clear()
        caplog.set_level(logging.ERROR, logger="pkv.gui.browser")

        browser_view.load_tags()

        assert browser_view._tag_model.rowCount() == previous_rows
        assert not browser_view._tag_view.isEnabled()
        assert not browser_view._tag_status_label.isHidden()
        assert browser_view._tag_status_label.text() == (
            "标签加载失败（错误代码：browser_tags_load_failed）"
        )
        public_text = browser_view._tag_status_label.text()
        assert "TAG-SECRET-CANARY" not in public_text
        assert "private" not in public_text
        assert "TAG-SECRET-CANARY" not in caplog.text
        assert "private" not in caplog.text
        assert "browser_tags_load_failed" in caplog.text
        assert "RuntimeError" in caplog.text

    @pytest.mark.parametrize(
        "malformed_tags",
        [
            pytest.param("", id="empty-string"),
            pytest.param({}, id="mapping-not-list"),
            pytest.param((), id="frozen-list"),
            pytest.param(
                [MappingProxyType({"name": "secret", "count": 1})],
                id="frozen-item",
            ),
            pytest.param([{"name": "missing-count"}], id="missing-count"),
            pytest.param([{"name": "bool-count", "count": True}], id="bool-count"),
        ],
    )
    def test_malformed_tag_projection_is_explicit_failure(
        self, browser_view, mock_store, malformed_tags
    ):
        previous_rows = browser_view._tag_model.rowCount()
        mock_store.get_all_tags_with_count.return_value = malformed_tags

        browser_view.load_tags()

        assert browser_view._tag_model.rowCount() == previous_rows
        assert not browser_view._tag_view.isEnabled()
        assert browser_view._tag_status_label.text() == (
            "标签加载失败（错误代码：browser_tags_load_failed）"
        )

    def test_exact_empty_tag_list_remains_a_valid_empty_state(
        self, browser_view, mock_store
    ):
        mock_store.get_all_tags_with_count.return_value = []

        browser_view.load_tags()

        assert browser_view._tag_model.rowCount() == 1
        assert browser_view._tag_view.isEnabled()
        assert browser_view._tag_status_label.isHidden()


# ============================================================
# 验收标准 2: 条目列表加载
# ============================================================

class TestEntryList:
    """测试条目列表加载。"""

    def test_entries_loaded_on_init(self, browser_view, mock_store):
        """初始化时加载条目。"""
        mock_store.list_entries.assert_called()

    def test_entry_model_has_correct_count(self, browser_view):
        """条目模型包含正确的条目数。"""
        model = browser_view._entry_model
        assert model.rowCount() == len(MOCK_ENTRIES)

    def test_entry_table_columns(self, browser_view):
        """条目表格有 6 列。"""
        model = browser_view._entry_model
        assert model.columnCount() == 6

    def test_entry_count_label_updated(self, browser_view):
        """条目计数标签被更新。"""
        label_text = browser_view._entry_count_label.text()
        assert "2" in label_text  # 总共 2 条

    def test_entry_data_display(self, browser_view):
        """条目显示数据正确。"""
        model = browser_view._entry_model
        # 第一行标题
        title_index = model.index(0, 1)  # COL_TITLE
        assert "深度学习" in model.data(title_index, Qt.DisplayRole)

    def test_get_entry_by_row(self, browser_view):
        """通过行号获取条目字典。"""
        entry = browser_view._entry_model.get_entry(0)
        assert entry is not None
        assert entry["knowledge_id"] == 1

    def test_get_entry_out_of_range(self, browser_view):
        """越界行号返回 None。"""
        entry = browser_view._entry_model.get_entry(999)
        assert entry is None

    @pytest.mark.parametrize(
        "malformed_entries",
        [
            pytest.param("", id="empty-string"),
            pytest.param({}, id="mapping-not-list"),
            pytest.param((), id="frozen-list"),
            pytest.param(
                [MappingProxyType(MOCK_ENTRIES[0])],
                id="frozen-item",
            ),
            pytest.param([{"knowledge_id": 1}], id="missing-fields"),
            pytest.param(
                [{**MOCK_ENTRIES[0], "knowledge_id": True}],
                id="bool-id",
            ),
            pytest.param(
                [{**MOCK_ENTRIES[0], "word_count": True}],
                id="bool-word-count",
            ),
        ],
    )
    def test_malformed_entry_projection_is_explicit_failure(
        self, browser_view, mock_store, malformed_entries
    ):
        mock_store.count_entries.return_value = 1
        mock_store.list_entries.return_value = malformed_entries

        browser_view.load_entries()

        assert browser_view._entry_model.rowCount() == 0
        assert not browser_view._entry_view.isEnabled()
        assert browser_view._entry_count_label.text() == "条目（加载失败）"
        assert browser_view._entry_status_label.text() == (
            "条目加载失败（错误代码：browser_entry_list_failed）"
        )

    def test_exact_empty_entry_list_remains_a_valid_empty_state(
        self, browser_view, mock_store
    ):
        mock_store.count_entries.return_value = 0
        mock_store.list_entries.return_value = []

        browser_view.load_entries()

        assert browser_view._entry_model.rowCount() == 0
        assert browser_view._entry_view.isEnabled()
        assert browser_view._entry_status_label.isHidden()
        assert browser_view._entry_count_label.text() == "条目 （共 0 条）"


# ============================================================
# 验收标准 2: 点击条目预览 Markdown
# ============================================================

class TestEntryPreview:
    """测试条目预览功能。"""

    def test_entry_click_loads_preview(self, browser_view):
        """点击条目触发预览加载。"""
        from src.gui.utils.preview_loader import PreviewOutcome

        outcome = PreviewOutcome(status="success", content="# 测试内容")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = browser_view._entry_model.index(0, 0)
                browser_view.on_entry_selected(index)
                # 验证预览区域有内容
                assert browser_view._preview_text.toPlainText() != ""
                assert browser_view._preview_status_label.isHidden()
                assert browser_view._send_to_chat_btn.isEnabled()

    def test_preview_title_updated(self, browser_view):
        """预览标题包含条目标题。"""
        from src.gui.utils.preview_loader import PreviewOutcome

        outcome = PreviewOutcome(status="success", content="内容")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = browser_view._entry_model.index(0, 0)
                browser_view.on_entry_selected(index)
                assert "深度学习" in browser_view._preview_title.text()

    def test_structured_degraded_preview_is_explicit(self, browser_view):
        """安全摘要必须伴随明确降级提示，不能伪装成正常正文。"""
        from src.gui.utils.preview_loader import PreviewIssue, PreviewOutcome
        from src.runtime.errors import ErrorCode

        outcome = PreviewOutcome(
            status="degraded",
            content="# 安全摘要",
            issue=PreviewIssue(
                code=ErrorCode.RESOURCE_MISSING,
                stage="preview_markdown",
                cause_type="FileNotFoundError",
            ),
        )
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ), patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
            index = browser_view._entry_model.index(0, 0)
            browser_view.on_entry_selected(index)

        assert browser_view._preview_text.toPlainText() == "# 安全摘要"
        assert "预览已降级" in browser_view._preview_status_label.text()
        assert "resource_missing" in browser_view._preview_status_label.text()
        assert not browser_view._preview_status_label.isHidden()
        assert browser_view._send_to_chat_btn.isEnabled()

    def test_preview_adapter_error_is_explicit_and_redacted(
        self, browser_view, caplog
    ):
        """adapter 异常显示失败态，且不回显路径或异常原文。"""
        canary = r"C:\private\preview.md api_key=PREVIEW-SECRET-CANARY"
        caplog.clear()
        caplog.set_level(logging.ERROR, logger="pkv.gui.browser")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            side_effect=RuntimeError(canary),
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = browser_view._entry_model.index(0, 0)
                browser_view.on_entry_selected(index)
                preview = browser_view._preview_text.toPlainText()
                assert preview == "（正文预览不可用）"
                assert "PREVIEW-SECRET-CANARY" not in preview
                assert "预览加载失败" in browser_view._preview_status_label.text()
                assert "browser_preview_adapter_failed" in (
                    browser_view._preview_status_label.text()
                )
                assert not browser_view._send_to_chat_btn.isEnabled()
        assert "PREVIEW-SECRET-CANARY" not in caplog.text
        assert "private" not in caplog.text
        assert "browser_preview_adapter_failed" in caplog.text
        assert "RuntimeError" in caplog.text

    def test_structured_preview_error_disables_send(self, browser_view):
        """loader 的显式 error issue 不得被当作可发送的摘要。"""
        from src.gui.utils.preview_loader import PreviewIssue, PreviewOutcome
        from src.runtime.errors import ErrorCode

        outcome = PreviewOutcome(
            status="error",
            content="",
            issue=PreviewIssue(
                code=ErrorCode.RESOURCE_NOT_READABLE,
                stage="preview_summary",
                cause_type="TypeError",
            ),
        )
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ), patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
            index = browser_view._entry_model.index(0, 0)
            browser_view.on_entry_selected(index)

        assert browser_view._preview_text.toPlainText() == "（正文预览不可用）"
        assert "resource_not_readable" in browser_view._preview_status_label.text()
        assert browser_view._selected_content == ""
        assert not browser_view._send_to_chat_btn.isEnabled()


class TestSendToChatLogging:
    """发送知识引用时不得把远端 title 写入日志。"""

    def test_title_crlf_secret_canary_is_not_logged(
        self, browser_view, qtbot, caplog
    ):
        canary = "Remote title\r\nAuthorization: Bearer SECRET-CANARY"
        browser_view._selected_entry = {
            "knowledge_id": 41,
            "title": canary,
        }
        browser_view._selected_content = "content"
        caplog.set_level(logging.INFO, logger="pkv.gui.browser")

        with qtbot.waitSignal(browser_view.send_to_chat_requested, timeout=1000):
            browser_view._on_send_to_chat()

        assert "SECRET-CANARY" not in caplog.text
        assert "Authorization" not in caplog.text
        assert "Remote title" not in caplog.text
        assert "knowledge_id=41" in caplog.text


# ============================================================
# 分页功能
# ============================================================

class TestPagination:
    """测试分页控件行为。"""

    def test_page_label_initial(self, browser_view):
        """初始页码显示正确。"""
        assert "第 1 页" in browser_view._page_label.text()

    def test_prev_button_disabled_on_first_page(self, browser_view):
        """第一页时上一页按钮禁用。"""
        assert not browser_view._prev_btn.isEnabled()

    def test_next_button_disabled_when_single_page(self, browser_view):
        """只有一页时下一页按钮禁用。"""
        # MOCK_ENTRIES 只有 2 条，PAGE_SIZE=20，所以只有 1 页
        assert not browser_view._next_btn.isEnabled()

    def test_pagination_with_many_entries(self, browser_view, mock_store):
        """多页数据时分页按钮正确启用。"""
        # 模拟 50 条数据
        mock_store.count_entries.return_value = 50
        mock_store.list_entries.return_value = _entry_page(20)

        browser_view.load_entries(page=0)
        # 50 / 20 = 3 页，下一页应可用
        assert browser_view._next_btn.isEnabled()
        assert not browser_view._prev_btn.isEnabled()

    def test_go_next_page(self, browser_view, mock_store):
        """点击下一页触发正确加载。"""
        mock_store.count_entries.return_value = 50
        mock_store.list_entries.return_value = _entry_page(20)

        browser_view.load_entries(page=0)
        browser_view._go_next_page()
        assert browser_view._current_page == 1

    def test_go_prev_page(self, browser_view, mock_store):
        """点击上一页触发正确加载。"""
        mock_store.count_entries.return_value = 50
        mock_store.list_entries.return_value = _entry_page(20)

        browser_view.load_entries(page=1)
        browser_view._go_prev_page()
        assert browser_view._current_page == 0

    def test_cannot_go_before_first_page(self, browser_view):
        """不能翻到第一页之前。"""
        browser_view._go_prev_page()
        assert browser_view._current_page == 0

    def test_list_failure_has_explicit_consistent_pagination_state(
        self, browser_view, mock_store, caplog
    ):
        """列表查询失败进入错误态，而不是伪装成零条结果或保留旧计数。"""
        canary = (
            r"C:\private\entries.sqlite"
            "\r\napi_key=LIST-SECRET-CANARY"
        )
        mock_store.count_entries.return_value = 50
        mock_store.list_entries.side_effect = RuntimeError(canary)
        browser_view._selected_entry = MOCK_ENTRIES[0]
        browser_view._selected_content = "stale"
        browser_view._send_to_chat_btn.setEnabled(True)
        caplog.clear()
        caplog.set_level(logging.ERROR, logger="pkv.gui.browser")

        browser_view.load_entries(page=2)

        assert browser_view._entry_model.rowCount() == 0
        assert browser_view._total_count == 0
        assert browser_view._current_page == 0
        assert browser_view._entry_count_label.text() == "条目（加载失败）"
        assert browser_view._entry_status_label.text() == (
            "条目加载失败（错误代码：browser_entry_list_failed）"
        )
        assert not browser_view._entry_status_label.isHidden()
        assert browser_view._page_label.text() == "分页不可用"
        assert not browser_view._prev_btn.isEnabled()
        assert not browser_view._next_btn.isEnabled()
        assert not browser_view._entry_view.isEnabled()
        assert browser_view._selected_entry is None
        assert browser_view._selected_content == ""
        assert not browser_view._send_to_chat_btn.isEnabled()
        public_text = " ".join([
            browser_view._entry_count_label.text(),
            browser_view._entry_status_label.text(),
            browser_view._page_label.text(),
            browser_view._preview_text.toPlainText(),
        ])
        assert "LIST-SECRET-CANARY" not in public_text
        assert "private" not in public_text
        assert "LIST-SECRET-CANARY" not in caplog.text
        assert "private" not in caplog.text
        assert "browser_entry_list_failed" in caplog.text
        assert "RuntimeError" in caplog.text

    def test_count_failure_skips_list_and_resets_pagination_safely(
        self, browser_view, mock_store, caplog
    ):
        """计数查询失败必须停止列表查询并清除旧分页投影。"""
        canary = (
            r"C:\private\count.sqlite"
            "\r\npassword=COUNT-SECRET-CANARY"
        )
        initial_list_calls = mock_store.list_entries.call_count
        mock_store.count_entries.side_effect = RuntimeError(canary)
        caplog.clear()
        caplog.set_level(logging.ERROR, logger="pkv.gui.browser")

        browser_view.load_entries(page=3)

        assert mock_store.list_entries.call_count == initial_list_calls
        assert browser_view._entry_model.rowCount() == 0
        assert browser_view._total_count == 0
        assert browser_view._current_page == 0
        assert browser_view._entry_count_label.text() == "条目（加载失败）"
        assert browser_view._entry_status_label.text() == (
            "条目加载失败（错误代码：browser_entry_count_failed）"
        )
        assert browser_view._page_label.text() == "分页不可用"
        assert not browser_view._prev_btn.isEnabled()
        assert not browser_view._next_btn.isEnabled()
        public_text = " ".join([
            browser_view._entry_count_label.text(),
            browser_view._entry_status_label.text(),
            browser_view._page_label.text(),
        ])
        assert "COUNT-SECRET-CANARY" not in public_text
        assert "private" not in public_text
        assert "COUNT-SECRET-CANARY" not in caplog.text
        assert "private" not in caplog.text
        assert "browser_entry_count_failed" in caplog.text
        assert "RuntimeError" in caplog.text

    def test_entry_error_state_recovers_after_success(self, browser_view, mock_store):
        """瞬时列表失败后成功刷新应恢复普通列表和分页状态。"""
        mock_store.list_entries.side_effect = RuntimeError("temporary")
        browser_view.load_entries(page=1)
        assert not browser_view._entry_status_label.isHidden()

        mock_store.list_entries.side_effect = None
        mock_store.count_entries.return_value = len(MOCK_ENTRIES)
        browser_view.load_entries(page=0)

        assert browser_view._entry_status_label.isHidden()
        assert browser_view._entry_view.isEnabled()
        assert browser_view._entry_model.rowCount() == len(MOCK_ENTRIES)
        assert browser_view._entry_count_label.text() == "条目 （共 2 条）"
        assert browser_view._page_label.text() == "第 1 页 / 共 1 页"

    @pytest.mark.parametrize(
        ("total_count", "page", "entries"),
        (
            pytest.param(0, 0, [MOCK_ENTRIES[0]], id="zero-total-with-row"),
            pytest.param(
                50,
                0,
                [dict(MOCK_ENTRIES[0]) for _ in range(21)],
                id="more-than-page-size",
            ),
            pytest.param(
                21,
                1,
                [dict(MOCK_ENTRIES[0]), dict(MOCK_ENTRIES[1])],
                id="rows-cross-total-boundary",
            ),
            pytest.param(1, 0, [], id="positive-total-with-empty-page"),
            pytest.param(
                50,
                0,
                [dict(MOCK_ENTRIES[0]), dict(MOCK_ENTRIES[1])],
                id="short-nonterminal-page",
            ),
            pytest.param(
                2,
                0,
                [dict(MOCK_ENTRIES[0]), dict(MOCK_ENTRIES[0])],
                id="duplicate-knowledge-id",
            ),
        ),
    )
    def test_incoherent_count_page_projection_is_explicit_failure(
        self,
        browser_view,
        mock_store,
        total_count,
        page,
        entries,
    ):
        mock_store.count_entries.return_value = total_count
        mock_store.list_entries.return_value = entries

        browser_view.load_entries(page=page)

        assert browser_view._entry_model.rowCount() == 0
        assert browser_view._total_count == 0
        assert browser_view._current_page == 0
        assert browser_view._entry_count_label.text() == "条目（加载失败）"
        assert browser_view._entry_status_label.text() == (
            "条目加载失败（错误代码：browser_entry_list_failed）"
        )
        assert not browser_view._entry_view.isEnabled()

    def test_requested_page_is_clamped_to_last_valid_offset(
        self, browser_view, mock_store
    ):
        mock_store.count_entries.return_value = 41
        mock_store.list_entries.return_value = [MOCK_ENTRIES[0]]

        browser_view.load_entries(page=999)

        assert mock_store.list_entries.call_args.kwargs["offset"] == 40
        assert browser_view._current_page == 2
        assert browser_view._entry_model.rowCount() == 1


# ============================================================
# 刷新功能
# ============================================================

class TestRefresh:
    """测试 BrowserView.refresh() 方法。"""

    def test_refresh_reloads_tags(self, browser_view, mock_store):
        """refresh() 重新加载标签。"""
        initial_tag_calls = mock_store.get_all_tags_with_count.call_count
        browser_view.refresh()
        assert mock_store.get_all_tags_with_count.call_count > initial_tag_calls

    def test_refresh_reloads_entries(self, browser_view, mock_store):
        """refresh() 重新加载条目列表。"""
        initial_list_calls = mock_store.list_entries.call_count
        browser_view.refresh()
        assert mock_store.list_entries.call_count > initial_list_calls

    def test_refresh_preserves_tag_filter(self, browser_view, mock_store):
        """refresh() 保留当前标签筛选状态。"""
        browser_view._current_tag = "AI"
        browser_view.refresh()
        last_call = mock_store.list_entries.call_args
        assert last_call.kwargs.get("tag") == "AI"

    def test_refresh_preserves_page(self, browser_view, mock_store):
        """refresh() 保留当前页码。"""
        mock_store.count_entries.return_value = 22
        browser_view._current_page = 1
        browser_view.refresh()
        assert browser_view._current_page == 1

    def test_refresh_updates_new_data(self, browser_view, mock_store):
        """refresh() 加载最新数据（模拟新增条目）。"""
        new_entries = MOCK_ENTRIES + [{
            "knowledge_id": 3,
            "title": "新归档条目",
            "source_type": "text",
            "tags": "测试",
            "word_count": 500,
            "archived_at": "2026-02-19 22:00:00",
            "file_path": "text/2026/02/20260219-new.md",
            "summary_one_sentence": "新归档的测试条目",
        }]
        mock_store.list_entries.return_value = new_entries
        mock_store.count_entries.return_value = len(new_entries)
        browser_view.refresh()
        assert browser_view._entry_model.rowCount() == 3
        assert browser_view._total_count == 3


# ============================================================
# 右键删除功能
# ============================================================

class TestDeleteFeature:
    """测试 BrowserView 删除功能。"""

    def test_context_menu_policy_set(self, browser_view):
        """条目表格启用了自定义右键菜单。"""
        from PySide6.QtCore import Qt
        assert browser_view._entry_view.contextMenuPolicy() == Qt.CustomContextMenu

    def test_execute_delete_refreshes_view(self, browser_view, mock_store):
        """删除条目后视图自动刷新。"""
        coordinator = MagicMock()
        coordinator.delete.return_value = StorageOperationResult(
            operation_id="delete-refresh",
            action="delete",
            status=OperationStatus.DELETED,
            stage=StorageStage.COMPLETED,
        )
        initial_list_calls = mock_store.list_entries.call_count

        entry = MOCK_ENTRIES[0]
        with patch(
            "src.gui.stores.get_storage_coordinator", return_value=coordinator
        ):
            browser_view._execute_delete(entry)

        # 验证 list_entries 被再次调用（refresh 触发）
        assert mock_store.list_entries.call_count > initial_list_calls

    def test_execute_delete_calls_storage_coordinator(self, browser_view):
        """GUI 删除只调用统一协调器，不绕过跨存储状态机。"""
        coordinator = MagicMock()
        coordinator.delete.return_value = StorageOperationResult(
            operation_id="delete-coordinator",
            action="delete",
            status=OperationStatus.DELETED,
            stage=StorageStage.COMPLETED,
        )

        entry = MOCK_ENTRIES[0]
        with patch(
            "src.gui.stores.get_storage_coordinator", return_value=coordinator
        ):
            browser_view._execute_delete(entry)

        coordinator.delete.assert_called_once()
        args, kwargs = coordinator.delete.call_args
        assert args == (entry["knowledge_id"],)
        assert callable(kwargs["vector_operation"])

    def test_execute_delete_calls_all_storage_layers(
        self,
        browser_view,
    ):
        """GUI 将向量删除回调交给协调器，由协调器决定执行时机。"""

        coordinator = MagicMock()
        vector_store = MagicMock()
        entry = MOCK_ENTRIES[0]

        def run_delete(knowledge_id, *, vector_operation):
            vector_operation(knowledge_id)
            return StorageOperationResult(
                operation_id="delete-all-layers",
                action="delete",
                status=OperationStatus.DELETED,
                stage=StorageStage.COMPLETED,
                knowledge_id=knowledge_id,
            )

        coordinator.delete.side_effect = run_delete

        with patch(
            "src.gui.stores.get_storage_coordinator",
            return_value=coordinator,
        ), patch(
            "src.gui.stores.get_vector_store",
            return_value=vector_store,
        ):
            browser_view._execute_delete(entry)

        coordinator.delete.assert_called_once()
        vector_store.delete_vectors_for_entry.assert_called_once_with(
            entry["knowledge_id"]
        )
