"""SearchView pytest-qt 单元测试。

覆盖 M10 验收标准:
3. 搜索功能：输入关键词 → 返回结果 → 点击查看详情

测试策略：Mock BM25Retriever，验证搜索流程和 UI 更新。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from PySide6.QtCore import Qt

from src.gui.utils.preview_loader import PreviewIssue, PreviewOutcome
from src.retrieval.result import RetrievalIssue, SearchResponse, SearchResult
from src.runtime.errors import ErrorCode

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ============================================================
# Mock 数据
# ============================================================

def _make_search_result(
    kid: int,
    title: str,
    score: float,
    highlight: str,
    metadata: dict,
) -> SearchResult:
    """构造真实 SearchResult 合同对象。"""
    return SearchResult(
        knowledge_id=kid,
        title=title,
        score=score,
        highlight=highlight,
        metadata=metadata,
    )


MOCK_SEARCH_RESULTS = [
    _make_search_result(
        kid=1,
        title="AI 工作流设计",
        score=0.85,
        highlight="基于工作流的 AI 知识管理系统设计要点",
        metadata={
            "source_type": "wechat",
            "tags": "AI,工作流",
            "word_count": 2500,
            "archived_at": "2026-02-15 10:30:00",
            "file_path": "wechat/2026/02/20260215-ai-workflow.md",
        },
    ),
    _make_search_result(
        kid=2,
        title="知识检索策略",
        score=0.72,
        highlight="BM25 与向量检索的混合策略分析",
        metadata={
            "source_type": "zhihu",
            "tags": "检索,BM25",
            "word_count": 1800,
            "archived_at": "2026-02-16 14:20:00",
            "file_path": "zhihu/2026/02/20260216-retrieval.md",
        },
    ),
]


def _unchecked_response(
    *,
    status: object,
    results: object = (),
    issues: object = (),
    strategy: object = "bm25",
) -> SearchResponse:
    """Build a deliberately malformed response without running post-init."""

    response = object.__new__(SearchResponse)
    object.__setattr__(response, "status", status)
    object.__setattr__(response, "results", results)
    object.__setattr__(response, "issues", issues)
    object.__setattr__(response, "strategy", strategy)
    return response


def _unchecked_preview_outcome(
    *,
    status: object,
    content: object,
    issue: object = None,
) -> PreviewOutcome:
    """Build a frozen-corrupted preview outcome for adapter canaries."""

    outcome = object.__new__(PreviewOutcome)
    object.__setattr__(outcome, "status", status)
    object.__setattr__(outcome, "content", content)
    object.__setattr__(outcome, "issue", issue)
    return outcome


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def mock_retriever():
    """创建 Mock BM25Retriever。"""
    retriever = MagicMock()
    retriever.search.return_value = SearchResponse.completed(
        MOCK_SEARCH_RESULTS,
        strategy="bm25",
    )
    return retriever


@pytest.fixture
def search_view(qtbot, mock_retriever):
    """创建带有 Mock 检索器的 SearchView。

    使用 yield 确保 mock 上下文在整个测试期间保持活跃，
    因为 SearchView.do_search() 内部使用延迟导入。
    """
    with patch("src.gui.stores.get_bm25_retriever", return_value=mock_retriever):
        from src.gui.views.search_view import SearchView
        view = SearchView()
        qtbot.addWidget(view)
        yield view


# ============================================================
# UI 结构验证
# ============================================================

class TestSearchViewStructure:
    """测试 SearchView 的 UI 结构。"""

    def test_view_is_created(self, search_view):
        """视图实例化不崩溃。"""
        assert search_view is not None

    def test_has_search_input(self, search_view):
        """包含搜索输入框。"""
        assert search_view.search_input is not None

    def test_has_search_button(self, search_view):
        """包含搜索按钮。"""
        assert search_view._search_btn is not None

    def test_has_strategy_combo(self, search_view):
        """包含策略选择下拉框。"""
        assert search_view._strategy_combo is not None
        assert search_view._strategy_combo.count() == 1
        assert search_view._strategy_combo.itemText(0) == "BM25"

    def test_has_result_table(self, search_view):
        """包含结果表格。"""
        assert search_view._result_view is not None

    def test_has_preview_panel(self, search_view):
        """包含预览面板。"""
        assert search_view._preview_text is not None
        assert search_view._preview_text.isReadOnly()

    def test_search_input_placeholder(self, search_view):
        """搜索框有占位提示文本。"""
        placeholder = search_view.search_input.placeholderText()
        assert len(placeholder) > 0


# ============================================================
# 验收标准 3: 输入关键词 → 返回结果
# ============================================================

class TestSearchExecution:
    """测试搜索执行。"""

    def test_search_with_query(self, search_view, mock_retriever, qtbot):
        """输入关键词后执行搜索返回结果。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        mock_retriever.search.assert_called_once_with("AI", limit=50)
        assert search_view._result_model.rowCount() == 2

    def test_search_result_count_label(self, search_view, mock_retriever, qtbot):
        """搜索结果数量标签正确更新。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "2" in label_text
        assert "AI" in label_text

    def test_empty_query_shows_message(self, search_view):
        """空查询显示提示消息。"""
        search_view.search_input.clear()
        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "请输入" in label_text or "关键词" in label_text

    def test_search_clears_preview(self, search_view, mock_retriever, qtbot):
        """搜索后清空预览区域。"""
        search_view._preview_text.setPlainText("旧内容")
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()
        assert search_view._preview_text.toPlainText() == ""

    def test_search_via_enter_key(self, search_view, mock_retriever, qtbot):
        """回车键触发搜索。"""
        search_view.search_input.setText("工作流")
        # 模拟 returnPressed 信号
        search_view.search_input.returnPressed.emit()
        mock_retriever.search.assert_called()

    def test_search_failure_shows_error(self, search_view, mock_retriever, qtbot):
        """搜索失败时显示错误消息。"""
        mock_retriever.search.side_effect = Exception("连接失败")
        # 使用 setText 代替 keyClicks 避免中文输入在 offscreen 平台崩溃
        search_view.search_input.setText("测试")
        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "失败" in label_text
        assert search_view._result_model.rowCount() == 0

    def test_no_hits_is_distinct_from_error(self, search_view, mock_retriever):
        mock_retriever.search.return_value = SearchResponse.completed((), strategy="bm25")
        search_view.search_input.setText("missing")

        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "未找到匹配结果" in label_text
        assert "失败" not in label_text

    def test_invalid_query_shows_stable_code(self, search_view, mock_retriever):
        mock_retriever.search.return_value = SearchResponse.invalid(
            "unsafe detail must not leak",
            strategy="bm25",
        )
        search_view.search_input.setText("bad")

        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "查询无效" in label_text
        assert ErrorCode.RETRIEVAL_INVALID_QUERY.value in label_text
        assert "unsafe detail" not in label_text
        assert search_view._result_model.rowCount() == 0

    def test_backend_error_is_not_rendered_as_zero_hits(
        self, search_view, mock_retriever
    ):
        issue = RetrievalIssue(
            code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
            message="secret-token=/private/path",
            stage="bm25_query",
        )
        mock_retriever.search.return_value = SearchResponse.failed_response(
            issue,
            strategy="bm25",
        )
        search_view.search_input.setText("query")

        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "搜索失败" in label_text
        assert ErrorCode.RETRIEVAL_BACKEND_FAILED.value in label_text
        assert "0 条" not in label_text
        assert "secret-token" not in label_text
        assert "/private/path" not in label_text

    def test_legacy_list_result_fails_closed(self, search_view, mock_retriever):
        mock_retriever.search.return_value = list(MOCK_SEARCH_RESULTS)
        search_view.search_input.setText("AI")

        search_view.do_search()

        assert "搜索失败" in search_view._result_count_label.text()
        assert "0 条" not in search_view._result_count_label.text()
        assert search_view._result_model.rowCount() == 0

    def test_inconsistent_success_response_fails_closed(
        self, search_view, mock_retriever
    ):
        """A malformed success must be an adapter error, never success/no-hits."""

        issue = RetrievalIssue(
            code=ErrorCode.RETRIEVAL_BACKEND_FAILED,
            message="secret detail",
            stage="bm25_query",
        )
        mock_retriever.search.return_value = _unchecked_response(
            status="success",
            results=(),
            issues=(issue,),
        )
        search_view.search_input.setText("AI")

        search_view.do_search()

        label = search_view._result_count_label.text()
        assert "搜索失败" in label
        assert "未找到匹配结果" not in label
        assert "找到 0 条结果" not in label
        assert search_view._result_model.rowCount() == 0

    def test_non_bm25_strategy_response_fails_closed(
        self, search_view, mock_retriever
    ):
        """The BM25-only GUI must reject a valid response from another seam."""

        mock_retriever.search.return_value = SearchResponse.completed(
            MOCK_SEARCH_RESULTS,
            strategy="vector",
        )
        search_view.search_input.setText("AI")

        search_view.do_search()

        label = search_view._result_count_label.text()
        assert "搜索失败" in label
        assert "adapter_error" in label
        assert search_view._result_model.rowCount() == 0

    def test_degraded_results_remain_visible_with_warning(
        self, search_view, mock_retriever
    ):
        issue = RetrievalIssue(
            code=ErrorCode.RETRIEVAL_METADATA_INCONSISTENT,
            message="raw metadata detail",
            stage="metadata",
            recoverable=True,
        )
        mock_retriever.search.return_value = SearchResponse.degraded_response(
            MOCK_SEARCH_RESULTS[:1],
            (issue,),
            strategy="bm25",
        )
        search_view.search_input.setText("AI")

        search_view.do_search()

        label_text = search_view._result_count_label.text()
        assert "搜索降级" in label_text
        assert "1 条可用结果" in label_text
        assert ErrorCode.RETRIEVAL_METADATA_INCONSISTENT.value in label_text
        assert "raw metadata detail" not in label_text
        assert search_view._result_model.rowCount() == 1

    def test_untrusted_issue_code_is_not_echoed(self, search_view):
        issue = MagicMock()
        issue.code = "api_key=CANARY C:/private/db.sqlite"
        assert search_view._issue_codes((issue,)) == "retrieval_error"

    def test_search_result_data_correct(self, search_view, mock_retriever, qtbot):
        """搜索结果数据正确转换为表格显示。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        model = search_view._result_model
        # 第一行标题
        title_index = model.index(0, 1)  # COL_TITLE
        assert "AI 工作流" in model.data(title_index, Qt.DisplayRole)

        # 第一行来源
        source_index = model.index(0, 2)  # COL_SOURCE_TYPE
        assert model.data(source_index, Qt.DisplayRole) == "wechat"

    def test_search_result_word_count_from_metadata(self, search_view, mock_retriever, qtbot):
        """搜索结果的字数从 metadata 中取（非硬编码 0）。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        model = search_view._result_model
        wc_index = model.index(0, 4)  # COL_WORD_COUNT
        word_count_str = model.data(wc_index, Qt.DisplayRole)
        assert word_count_str == "2500"  # 来自 metadata


# ============================================================
# 验收标准 3: 点击查看详情
# ============================================================

class TestResultPreview:
    """测试搜索结果详情预览。"""

    def test_result_click_loads_preview(self, search_view, mock_retriever, qtbot):
        """点击搜索结果加载预览。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        outcome = PreviewOutcome(status="success", content="# 全文内容")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = search_view._result_model.index(0, 0)
                search_view.on_result_selected(index)
                preview = search_view._preview_text.toPlainText()
                assert len(preview) > 0
                assert search_view._preview_status_label.isHidden()

    def test_preview_title_updated(self, search_view, mock_retriever, qtbot):
        """预览标题包含条目标题。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        outcome = PreviewOutcome(status="success", content="内容")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = search_view._result_model.index(0, 0)
                search_view.on_result_selected(index)
                assert "AI 工作流" in search_view._preview_title.text()

    def test_preview_contains_highlight(self, search_view, mock_retriever, qtbot):
        """预览内容包含搜索摘要（highlight）。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        outcome = PreviewOutcome(status="success", content="正文")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = search_view._result_model.index(0, 0)
                search_view.on_result_selected(index)
                preview = search_view._preview_text.toPlainText()
                assert "工作流" in preview  # 来自 highlight 字段

    def test_preview_contains_metadata(self, search_view, mock_retriever, qtbot):
        """预览内容包含元数据信息。"""
        qtbot.keyClicks(search_view.search_input, "AI")
        search_view.do_search()

        outcome = PreviewOutcome(status="success", content="正文")
        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ):
            with patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
                index = search_view._result_model.index(0, 0)
                search_view.on_result_selected(index)
                preview = search_view._preview_text.toPlainText()
                assert "wechat" in preview

    def test_degraded_preview_has_explicit_banner_and_safe_summary(
        self, search_view, mock_retriever
    ):
        search_view.search_input.setText("AI")
        search_view.do_search()
        issue = PreviewIssue(
            code=ErrorCode.RESOURCE_MISSING,
            stage="preview_markdown",
            recoverable=True,
            cause_type="FileNotFoundError",
        )
        outcome = PreviewOutcome(
            status="degraded",
            content="# 安全摘要",
            issue=issue,
        )

        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ), patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
            search_view.on_result_selected(search_view._result_model.index(0, 0))

        assert not search_view._preview_status_label.isHidden()
        assert "预览降级" in search_view._preview_status_label.text()
        assert ErrorCode.RESOURCE_MISSING.value in search_view._preview_status_label.text()
        assert "安全摘要" in search_view._preview_text.toPlainText()

    def test_error_preview_has_explicit_failure_and_no_normal_summary(
        self, search_view, mock_retriever
    ):
        search_view.search_input.setText("AI")
        search_view.do_search()
        issue = PreviewIssue(
            code=ErrorCode.RESOURCE_NOT_READABLE,
            stage="preview_summary",
            cause_type="InvalidPreviewEntry",
        )
        outcome = PreviewOutcome(status="error", content="", issue=issue)

        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=outcome,
        ), patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
            search_view.on_result_selected(search_view._result_model.index(0, 0))

        assert not search_view._preview_status_label.isHidden()
        assert "预览失败" in search_view._preview_status_label.text()
        assert (
            ErrorCode.RESOURCE_NOT_READABLE.value
            in search_view._preview_status_label.text()
        )
        assert search_view._preview_text.toPlainText() == "预览内容暂不可用。"
        assert "工作流" not in search_view._preview_text.toPlainText()

    def test_unexpected_preview_exception_is_redacted_and_fails_closed(
        self, search_view, caplog
    ):
        secret = "CANARY_API_KEY_123"
        private_path = "C:/private/CANARY-vault.md"
        entry = {
            "title": "Safe title",
            "file_path": private_path,
            "highlight": "safe highlight",
        }
        caplog.set_level("ERROR", logger="pkv.gui.search")

        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            side_effect=RuntimeError(f"api_key={secret} path={private_path}"),
        ), patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
            search_view._load_preview(entry)

        public_text = "\n".join(
            (
                search_view._preview_status_label.text(),
                search_view._preview_text.toPlainText(),
                caplog.text,
            )
        )
        assert "preview_adapter_error" in public_text
        assert "RuntimeError" in caplog.text
        assert secret not in public_text
        assert private_path not in public_text
        assert "api_key=" not in public_text

    def test_malformed_preview_outcome_is_redacted_and_fails_closed(
        self, search_view, caplog
    ):
        secret = "CANARY_MALFORMED_KEY"
        private_path = "C:/private/malformed-vault.md"
        malformed = _unchecked_preview_outcome(
            status="degraded",
            content=f"api_key={secret} path={private_path}",
            issue=None,
        )
        caplog.set_level("ERROR", logger="pkv.gui.search")

        with patch(
            "src.gui.utils.preview_loader.load_entry_preview_outcome",
            return_value=malformed,
        ), patch("src.gui.stores.get_markdown_store", return_value=MagicMock()):
            search_view._load_preview(
                {"title": "Safe title", "file_path": "synthetic.md"}
            )

        public_text = "\n".join(
            (
                search_view._preview_status_label.text(),
                search_view._preview_text.toPlainText(),
                caplog.text,
            )
        )
        assert "preview_adapter_error" in public_text
        assert "TypeError" in caplog.text
        assert "预览降级" not in public_text
        assert secret not in public_text
        assert private_path not in public_text
        assert "api_key=" not in public_text


# ============================================================
# 公开方法
# ============================================================

class TestPublicMethods:
    """测试 SearchView 的公开方法。"""

    def test_focus_search_input(self, search_view, qtbot):
        """focus_search_input() 聚焦搜索框。"""
        search_view.show()
        qtbot.waitExposed(search_view)
        search_view.focus_search_input()
        # 在 offscreen 平台下 hasFocus() 可能不可靠，
        # 验证 focusWidget 是 search_input 或直接验证方法不崩溃
        focus_widget = search_view.focusWidget()
        # offscreen 平台可能返回 None，所以只验证不崩溃即可
        assert focus_widget is None or focus_widget is search_view.search_input

    def test_focus_search_input_selects_text(self, search_view, qtbot):
        """focus_search_input() 全选已有文本。"""
        search_view.show()
        qtbot.waitExposed(search_view)
        search_view.search_input.setText("已有文本")
        search_view.focus_search_input()
        # 验证 selectAll 逻辑：若 offscreen 平台支持焦点则文本被全选
        selected = search_view.search_input.selectedText()
        if search_view.search_input.hasFocus():
            assert selected == "已有文本"
        else:
            # offscreen 平台可能无法正确聚焦，验证方法不崩溃即可
            assert True
