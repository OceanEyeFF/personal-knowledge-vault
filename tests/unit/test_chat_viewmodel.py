"""ChatViewModel 单元测试 (M12)

测试 src.gui.viewmodels.chat_viewmodel 中的核心方法：
- set_knowledge_context: 设置/替换 system message
- create_new_session: 创建新会话
- load_session: 加载已有会话
- list_sessions: 列出会话
- stop_stream: 停止流式输出
- delete_current_session: 删除当前会话
- archive_current_session: 归档当前会话
- get_token_stats: 获取 Token 统计
- get_current_messages: 获取消息列表
"""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import DefaultAsyncHttpxClient as SDKDefaultAsyncHttpxClient


def _list_session_row(**overrides):
    row = {
        "session_id": "session-1",
        "title": "对话 1",
        "created_at": "2026-08-07 10:00:00",
        "updated_at": "2026-08-07 10:01:00",
        "total_tokens": 12,
        "round_count": 2,
        "is_archived": 0,
        "knowledge_id": None,
        "summary": None,
    }
    row.update(overrides)
    return row


def _list_session_row_without(field):
    row = _list_session_row()
    row.pop(field)
    return row


def _kb_session_row(session_id, title, messages, **overrides):
    row = {
        "session_id": session_id,
        "title": title,
        "messages": messages,
        "total_tokens": 0,
        "round_count": 0,
    }
    row.update(overrides)
    return row


def _kb_success_payload(
    knowledge_id=73,
    *,
    terminal="success",
    status="ready",
    **overrides,
):
    payload = {
        "knowledge_id": knowledge_id,
        "title": "保存的对话",
        "file_path": "vault/saved-chat.md",
        "status": status,
        "operation_id": f"{knowledge_id:032x}",
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": (
            ["rebuild_vectors_for_entry"] if status == "degraded" else []
        ),
        "workflow_terminal": terminal,
        "workflow_warnings": (
            ["工作流存在降级警告"] if terminal == "degraded" else []
        ),
        "workflow_issues": [],
    }
    payload.update(overrides)
    return payload


def _url_completion_data(knowledge_id, *, status="ready", **overrides):
    data = {
        "knowledge_id": knowledge_id,
        "status": status,
        "operation_id": f"{knowledge_id:032x}",
        "core_committed": True,
        "do_not_retry": True,
        "repair_actions": (
            ["rebuild_vectors_for_entry"] if status == "degraded" else []
        ),
    }
    data.update(overrides)
    return data


# Mock 所有外部依赖，避免真实初始化
@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    """Mock Config 和 SQLiteStore，避免真实 DB 初始化"""
    mock_config = MagicMock()
    mock_config.db_path = ".data-test/db/runtime.db"
    mock_config.llm_api_key = "fake-api-key"
    mock_config.llm_provider = "openai_compatible"
    mock_config.llm_base_url = "https://llm.example/v1"
    mock_config.llm_model = "configured-model"
    mock_config.llm_max_tokens = 2000
    mock_config.llm_temperature = 0.7
    mock_config.llm_timeout_seconds = 30.0
    mock_config.llm_max_retries = 2

    config_attributes = {
        "ai.llm.api_key": "llm_api_key",
        "ai.llm.provider": "llm_provider",
        "ai.llm.base_url": "llm_base_url",
        "ai.llm.model": "llm_model",
        "ai.llm.max_tokens": "llm_max_tokens",
        "ai.llm.temperature": "llm_temperature",
        "ai.llm.timeout_seconds": "llm_timeout_seconds",
        "ai.llm.max_retries": "llm_max_retries",
    }

    def get_config_value(key, default=None):
        attribute = config_attributes.get(key)
        return getattr(mock_config, attribute) if attribute else default

    mock_config.get.side_effect = get_config_value

    mock_store = MagicMock()
    mock_store.create_session.return_value = None
    mock_store.get_session.return_value = None
    mock_store.list_sessions.return_value = []

    def update_session(**payload):
        mock_store.get_session.return_value = {
            "session_id": payload["session_id"],
            "messages": payload["messages"],
            "total_tokens": payload["total_tokens"],
            "round_count": payload["round_count"],
        }

    mock_store.update_session.side_effect = update_session
    mock_store.delete_session.return_value = True
    mock_store.archive_session.return_value = None

    with patch("src.gui.viewmodels.chat_viewmodel.Config", return_value=mock_config):
        with patch(
            "src.gui.viewmodels.chat_viewmodel.SQLiteStore",
            return_value=mock_store,
        ):
            yield {"config": mock_config, "store": mock_store}


@pytest.fixture
def viewmodel(mock_dependencies):
    """创建 ChatViewModel 实例（依赖已 Mock）"""
    from src.gui.viewmodels.chat_viewmodel import ChatViewModel

    vm = ChatViewModel()
    return vm


def test_initialization_uses_runtime_db_path(viewmodel, mock_dependencies) -> None:
    """数据库连接应使用支持 DATA_DIR/DB_PATH 覆盖的 Config.db_path。"""
    assert viewmodel.db_path == ".data-test/db/runtime.db"
    mock_dependencies["config"].get.assert_not_called()


def test_reload_provider_config_replaces_source_for_next_snapshot(
    viewmodel, mock_dependencies
) -> None:
    """设置保存后应刷新下一次对话请求使用的 Provider 配置。"""
    config = mock_dependencies["config"]
    config.llm_api_key = "updated-key"
    config.llm_base_url = "https://updated.example.com/v1"
    config.llm_model = "updated-model"

    viewmodel.reload_provider_config()

    from src.ai.provider_factory import chat_settings_from_config

    assert viewmodel.config is config
    settings = chat_settings_from_config(viewmodel.config)
    assert settings.api_key == "updated-key"
    assert settings.base_url == "https://updated.example.com/v1"
    assert settings.model == "updated-model"


# ===================================================================
# set_knowledge_context
# ===================================================================


class TestSetKnowledgeContext:
    """设置知识引用上下文测试"""

    def test_empty_context_ignored(self, viewmodel) -> None:
        """空上下文不做任何处理"""
        viewmodel.current_messages = [{"role": "user", "content": "hello"}]
        viewmodel.set_knowledge_context("")
        # 消息列表不变
        assert len(viewmodel.current_messages) == 1
        assert viewmodel.current_messages[0]["role"] == "user"

    def test_insert_system_message(self, viewmodel) -> None:
        """插入 system message 到列表开头"""
        viewmodel.current_messages = [{"role": "user", "content": "hello"}]
        viewmodel.set_knowledge_context("参考以下知识库内容...")

        assert len(viewmodel.current_messages) == 2
        assert viewmodel.current_messages[0]["role"] == "system"
        assert viewmodel.current_messages[0]["content"] == "参考以下知识库内容..."
        assert viewmodel.current_messages[1]["role"] == "user"

    def test_replace_existing_system_message(self, viewmodel) -> None:
        """替换已有的 system message"""
        viewmodel.current_messages = [
            {"role": "system", "content": "旧的上下文"},
            {"role": "user", "content": "hello"},
        ]
        viewmodel.set_knowledge_context("新的上下文")

        # 应该只有一个 system message
        system_msgs = [
            m for m in viewmodel.current_messages if m["role"] == "system"
        ]
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "新的上下文"

    def test_empty_messages_with_context(self, viewmodel) -> None:
        """空消息列表中插入上下文"""
        viewmodel.current_messages = []
        viewmodel.set_knowledge_context("引用内容")

        assert len(viewmodel.current_messages) == 1
        assert viewmodel.current_messages[0]["role"] == "system"


# ===================================================================
# create_new_session
# ===================================================================


class TestCreateNewSession:
    """创建新会话测试"""

    def test_creates_session_with_default_title(self, viewmodel) -> None:
        """使用默认标题创建会话"""
        session_id = viewmodel.create_new_session()
        assert session_id is not None
        assert len(session_id) > 0
        assert viewmodel.current_session_id == session_id
        assert viewmodel.current_messages == []
        assert viewmodel.current_total_tokens == 0
        assert viewmodel.current_round_count == 0

    def test_creates_session_with_custom_title(self, viewmodel) -> None:
        """使用自定义标题创建会话"""
        session_id = viewmodel.create_new_session(title="测试对话")
        assert session_id is not None
        assert viewmodel.current_session_id == session_id

    def test_resets_state_on_new_session(self, viewmodel) -> None:
        """新会话重置所有状态"""
        # 先设置一些状态
        viewmodel.current_messages = [{"role": "user", "content": "old"}]
        viewmodel.current_total_tokens = 500
        viewmodel.current_round_count = 3

        viewmodel.create_new_session()
        assert viewmodel.current_messages == []
        assert viewmodel.current_total_tokens == 0
        assert viewmodel.current_round_count == 0

    def test_emits_session_created_signal(self, viewmodel, qtbot) -> None:
        """创建会话时发射 session_created 信号"""
        with qtbot.waitSignal(viewmodel.session_created, timeout=1000):
            viewmodel.create_new_session(title="信号测试")


# ===================================================================
# load_session
# ===================================================================


class TestLoadSession:
    """加载会话测试"""

    def test_load_nonexistent_session(self, viewmodel) -> None:
        """加载不存在的会话"""
        result = viewmodel.load_session("nonexistent-id")
        assert result is False

    def test_load_existing_session(self, viewmodel, mock_dependencies) -> None:
        """加载已有会话"""
        mock_dependencies["store"].get_session.return_value = {
            "session_id": "test-session-id",
            "messages": [{"role": "user", "content": "hello"}],
            "total_tokens": 100,
            "round_count": 2,
        }

        result = viewmodel.load_session("test-session-id")
        assert result is True
        assert viewmodel.current_session_id == "test-session-id"
        assert len(viewmodel.current_messages) == 1
        assert viewmodel.current_total_tokens == 100
        assert viewmodel.current_round_count == 2

    def test_emits_session_loaded_signal(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        """加载会话时发射 session_loaded 信号"""
        mock_dependencies["store"].get_session.return_value = {
            "session_id": "signal-test",
            "messages": [],
            "total_tokens": 0,
            "round_count": 0,
        }

        with qtbot.waitSignal(viewmodel.session_loaded, timeout=1000):
            viewmodel.load_session("signal-test")

    def test_load_session_error(self, viewmodel, mock_dependencies) -> None:
        """加载会话异常"""
        mock_dependencies["store"].get_session.side_effect = RuntimeError("DB error")
        result = viewmodel.load_session("error-id")
        assert result is False


# ===================================================================
# list_sessions
# ===================================================================


class TestListSessions:
    """列出会话测试"""

    def test_list_empty(self, viewmodel) -> None:
        """空列表"""
        result = viewmodel.list_sessions()
        assert result == []

    def test_list_with_sessions(self, viewmodel, mock_dependencies) -> None:
        """有会话时返回列表"""
        mock_dependencies["store"].list_sessions.return_value = [
            _list_session_row(session_id="s1", title="对话1"),
            _list_session_row(
                session_id="s2",
                title="对话2",
                is_archived=1,
                knowledge_id=42,
                summary="摘要",
            ),
        ]
        result = viewmodel.list_sessions()
        assert result is not None
        assert len(result) == 2

    def test_list_archived(self, viewmodel, mock_dependencies) -> None:
        """列出归档会话"""
        viewmodel.list_sessions(is_archived=True)
        mock_dependencies["store"].list_sessions.assert_called_with(is_archived=True)

    def test_list_sessions_error(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        """空列表必须伴随安全可见错误，不能伪装成 no sessions。"""
        sentinel = "database-path-secret"
        mock_dependencies["store"].list_sessions.side_effect = RuntimeError(sentinel)
        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000) as blocker:
            result = viewmodel.list_sessions()
        assert result is None
        assert blocker.args == ["列出会话失败，请检查本地数据库状态"]
        assert sentinel not in blocker.args[0]

    @pytest.mark.parametrize(
        "projection",
        [
            None,
            (),
            MappingProxyType({}),
            [MappingProxyType(_list_session_row())],
            [_list_session_row_without("summary")],
            [_list_session_row(extra_field="unexpected")],
            [_list_session_row(session_id="")],
            [_list_session_row(title="   ")],
            [_list_session_row(created_at=None)],
            [_list_session_row(updated_at=[])],
            [_list_session_row(total_tokens=True)],
            [_list_session_row(total_tokens=-1)],
            [_list_session_row(round_count=True)],
            [_list_session_row(round_count=-1)],
            [_list_session_row(is_archived=True)],
            [_list_session_row(is_archived=2)],
            [_list_session_row(knowledge_id=True)],
            [_list_session_row(knowledge_id=0)],
            [_list_session_row(summary=["session-list-canary-secret"])],
        ],
        ids=[
            "none-root",
            "tuple-root",
            "mapping-root",
            "mapping-row",
            "missing-field",
            "extra-field",
            "empty-id",
            "blank-title",
            "created-at-type",
            "updated-at-type",
            "bool-total-tokens",
            "negative-total-tokens",
            "bool-round-count",
            "negative-round-count",
            "bool-archived",
            "invalid-archived",
            "bool-knowledge-id",
            "invalid-knowledge-id",
            "summary-type-canary",
        ],
    )
    def test_malformed_projection_is_failure_not_empty(
        self,
        projection,
        viewmodel,
        mock_dependencies,
        qtbot,
        caplog,
    ) -> None:
        mock_dependencies["store"].list_sessions.return_value = projection

        with caplog.at_level(logging.ERROR, logger="pkv.gui.viewmodels.chat"):
            with qtbot.waitSignal(
                viewmodel.error_occurred,
                timeout=1000,
            ) as blocker:
                result = viewmodel.list_sessions()

        assert result is None
        assert blocker.args == ["列出会话失败，请检查本地数据库状态"]
        assert "session-list-canary-secret" not in blocker.args[0]
        assert "session-list-canary-secret" not in caplog.text

    def test_rejects_non_boolean_archive_filter(
        self,
        viewmodel,
        mock_dependencies,
        qtbot,
    ) -> None:
        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000):
            assert viewmodel.list_sessions(is_archived=1) is None
        mock_dependencies["store"].list_sessions.assert_not_called()


class TestSessionSidebarProjection:
    """侧栏只在完整投影通过验证后执行替换。"""

    @pytest.mark.parametrize(
        "malformed",
        [
            (),
            MappingProxyType({}),
            [
                _list_session_row(
                    summary=["sidebar-session-list-canary-secret"]
                )
            ],
        ],
        ids=["tuple-root", "mapping-root", "malformed-row"],
    )
    def test_malformed_projection_preserves_existing_rows(
        self,
        malformed,
        qtbot,
    ) -> None:
        from src.gui.views.chat_view import SessionSidebar

        sidebar = SessionSidebar()
        qtbot.addWidget(sidebar)
        assert sidebar.load_sessions([_list_session_row()]) is True
        old_text = sidebar.session_list.item(0).text()
        old_id = sidebar.session_list.item(0).data(256)

        assert sidebar.load_sessions(malformed) is False
        assert sidebar.session_list.count() == 1
        assert sidebar.session_list.item(0).text() == old_text
        assert sidebar.session_list.item(0).data(256) == old_id

    def test_exact_empty_projection_clears_existing_rows(self, qtbot) -> None:
        from src.gui.views.chat_view import SessionSidebar

        sidebar = SessionSidebar()
        qtbot.addWidget(sidebar)
        assert sidebar.load_sessions([_list_session_row()]) is True
        assert sidebar.load_sessions([]) is True
        assert sidebar.session_list.count() == 0

    def test_backend_exception_keeps_sidebar_unchanged(
        self,
        viewmodel,
        mock_dependencies,
        qtbot,
    ) -> None:
        from src.gui.views.chat_view import ChatView, SessionSidebar

        sidebar = SessionSidebar()
        qtbot.addWidget(sidebar)
        assert sidebar.load_sessions([_list_session_row()]) is True
        mock_dependencies["store"].list_sessions.side_effect = RuntimeError(
            "sidebar-backend-canary-secret"
        )
        view = SimpleNamespace(viewmodel=viewmodel, sidebar=sidebar)

        assert ChatView._load_sessions(view) is False
        assert sidebar.session_list.count() == 1
        assert sidebar.session_list.item(0).data(256) == "session-1"


# ===================================================================
# stop_stream
# ===================================================================


class TestStopStream:
    """停止流式输出测试"""

    def test_stop_without_active_request_is_noop(self, viewmodel) -> None:
        """无活动请求时停止是幂等 no-op。"""
        assert viewmodel._stop_flag is False
        assert viewmodel.stop_stream() is False
        assert viewmodel._stop_flag is False


# ===================================================================
# delete_current_session
# ===================================================================


class TestDeleteCurrentSession:
    """删除当前会话测试"""

    def test_no_current_session(self, viewmodel) -> None:
        """无当前会话时返回 False"""
        viewmodel.current_session_id = None
        assert viewmodel.delete_current_session() is False

    def test_delete_success(self, viewmodel) -> None:
        """删除成功"""
        viewmodel.current_session_id = "to-delete"
        viewmodel.current_messages = [{"role": "user", "content": "x"}]
        viewmodel.current_total_tokens = 100

        result = viewmodel.delete_current_session()
        assert result is True
        assert viewmodel.current_session_id is None
        assert viewmodel.current_messages == []
        assert viewmodel.current_total_tokens == 0
        assert viewmodel.current_round_count == 0

    def test_delete_error(self, viewmodel, mock_dependencies) -> None:
        """删除异常"""
        viewmodel.current_session_id = "to-delete"
        mock_dependencies["store"].delete_session.side_effect = RuntimeError("err")
        result = viewmodel.delete_current_session()
        assert result is False

    def test_delete_other_session_keeps_current(self, viewmodel) -> None:
        """删除非当前会话时保留当前状态"""
        viewmodel.current_session_id = "current-id"
        viewmodel.current_messages = [{"role": "user", "content": "hi"}]
        viewmodel.current_total_tokens = 50

        result = viewmodel.delete_session("other-id")
        assert result is True
        # 当前会话状态不受影响
        assert viewmodel.current_session_id == "current-id"
        assert viewmodel.current_messages == [{"role": "user", "content": "hi"}]
        assert viewmodel.current_total_tokens == 50

    def test_rejects_deleting_active_request_session(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        viewmodel.current_session_id = "active-id"
        viewmodel._active_request = MagicMock(session_id="active-id")

        with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000) as blocker:
            result = viewmodel.delete_session("active-id")

        assert result is False
        assert "chat_state_conflict" in blocker.args[0]
        mock_dependencies["store"].delete_session.assert_not_called()


# ===================================================================
# archive_current_session
# ===================================================================


class TestArchiveCurrentSession:
    """归档当前会话测试"""

    def test_no_current_session(self, viewmodel) -> None:
        """无当前会话时返回 False"""
        viewmodel.current_session_id = None
        assert viewmodel.archive_current_session() is False

    def test_archive_success(self, viewmodel) -> None:
        """归档成功"""
        viewmodel.current_session_id = "to-archive"
        result = viewmodel.archive_current_session()
        assert result is True

    def test_archive_error(self, viewmodel, mock_dependencies) -> None:
        """归档异常"""
        viewmodel.current_session_id = "to-archive"
        mock_dependencies["store"].archive_session.side_effect = RuntimeError("err")
        result = viewmodel.archive_current_session()
        assert result is False


# ===================================================================
# get_token_stats / get_current_messages
# ===================================================================


class TestGetters:
    """Getter 方法测试"""

    def test_get_token_stats(self, viewmodel) -> None:
        """获取 Token 统计"""
        viewmodel.current_total_tokens = 1500
        viewmodel.current_round_count = 5
        stats = viewmodel.get_token_stats()
        assert stats["total_tokens"] == 1500
        assert stats["round_count"] == 5

    def test_get_current_messages(self, viewmodel) -> None:
        """获取当前消息列表"""
        messages = [{"role": "user", "content": "test"}]
        viewmodel.current_messages = messages
        assert viewmodel.get_current_messages() == messages

    def test_get_current_messages_empty(self, viewmodel) -> None:
        """空消息列表"""
        assert viewmodel.get_current_messages() == []


# ===================================================================
# create_new_session 异常分支
# ===================================================================


class TestCreateNewSessionError:
    """创建新会话异常测试"""

    def test_create_session_db_error(self, viewmodel, mock_dependencies) -> None:
        """数据库异常时 raise 并发射 error_occurred"""
        mock_dependencies["store"].create_session.side_effect = RuntimeError(
            "DB error"
        )
        with pytest.raises(RuntimeError, match="DB error"):
            viewmodel.create_new_session()


# ===================================================================
# send_message (async)
# ===================================================================


class TestSendMessage:
    """发送消息测试（async）"""

    def test_send_without_session(self, viewmodel, qtbot) -> None:
        """无当前会话时发射带稳定 code 的 Chat error。"""
        viewmodel.current_session_id = None

        # 直接调用内部 async 逻辑
        loop = asyncio.new_event_loop()
        try:
            # send_message 是 @asyncSlot 装饰的，直接调用其 coro
            with qtbot.waitSignal(
                viewmodel.chat_request_rejected, timeout=1000
            ) as blocker:
                loop.run_until_complete(
                    viewmodel.send_message.__wrapped__(viewmodel, "hello")
                )
        finally:
            loop.close()
        assert blocker.args[0] == ""
        assert blocker.args[2] == "chat_state_conflict"

    def test_send_without_api_key(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        """无 API Key 时发射 error_occurred"""
        viewmodel.current_session_id = "test-id"
        mock_dependencies["config"].llm_api_key = None

        loop = asyncio.new_event_loop()
        try:
            with qtbot.waitSignal(
                viewmodel.chat_request_rejected, timeout=1000
            ) as blocker:
                loop.run_until_complete(
                    viewmodel.send_message.__wrapped__(viewmodel, "hello")
                )
        finally:
            loop.close()
        assert blocker.args[2] == "provider_config_invalid"

    def test_send_message_stream_success(self, viewmodel, mock_dependencies) -> None:
        """流式发送成功"""
        viewmodel.current_session_id = "test-id"

        # 构造 Mock stream
        mock_chunk1 = MagicMock()
        mock_chunk1.choices = [MagicMock()]
        mock_chunk1.choices[0].delta.content = "Hello"
        mock_chunk1.choices[0].finish_reason = None
        mock_chunk1.usage = None

        mock_chunk2 = MagicMock()
        mock_chunk2.choices = [MagicMock()]
        mock_chunk2.choices[0].delta.content = " World"
        mock_chunk2.choices[0].finish_reason = "stop"
        mock_chunk2.usage = None

        # 最后一个 chunk 包含 usage
        mock_chunk3 = MagicMock()
        mock_chunk3.choices = []
        mock_chunk3.usage = MagicMock()
        mock_chunk3.usage.prompt_tokens = 10
        mock_chunk3.usage.completion_tokens = 5

        class MockStream:
            def __init__(self, chunks):
                self._chunks = chunks
                self.close = AsyncMock()

            def __aiter__(self):
                return self._iterate()

            async def _iterate(self):
                for chunk in self._chunks:
                    yield chunk

        mock_stream = MockStream([mock_chunk1, mock_chunk2, mock_chunk3])
        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_stream)

        received_tokens = []
        errors = []
        finished = []

        def on_token(t):
            received_tokens.append(t)

        viewmodel.token_received.connect(on_token)
        viewmodel.error_occurred.connect(errors.append)
        viewmodel.stream_finished.connect(lambda: finished.append(True))

        loop = asyncio.new_event_loop()
        try:
            with patch(
                "openai.AsyncOpenAI",
                return_value=mock_client,
            ):
                loop.run_until_complete(
                    viewmodel.send_message.__wrapped__(viewmodel, "test message")
                )
        finally:
            loop.close()

        # 验证 user message 被添加
        assert any(
            m["role"] == "user" and m["content"] == "test message"
            for m in viewmodel.current_messages
        )
        assert received_tokens == ["Hello", " World"]
        assert viewmodel.current_messages[-1] == {
            "role": "assistant",
            "content": "Hello World",
        }
        assert viewmodel.current_total_tokens == 15
        assert finished == [True]
        assert errors == []
        mock_stream.close.assert_awaited_once()
        mock_dependencies["store"].update_session.assert_called_once()

    def test_send_message_real_transport_preserves_endpoint_query_semantics(
        self, viewmodel
    ) -> None:
        """AsyncOpenAI 最终请求正确追加 path，并保留重复/空 query。"""
        viewmodel.current_session_id = "test-id"
        viewmodel.config.llm_api_key = "fake-key"
        viewmodel.config.llm_model = "configured-model"
        viewmodel.config.llm_base_url = (
            "https://chat.example/v1?region_code=north&region_code=south"
            "&flag=&routing_key=primary#client-only"
        )
        request_urls = []
        event_stream = (
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
            '"created":1,"model":"configured-model","choices":['
            '{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
            '"created":1,"model":"configured-model","choices":[],"usage":'
            '{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )

        def handle_request(request: httpx.Request) -> httpx.Response:
            request_urls.append(str(request.url))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=event_stream.encode("utf-8"),
            )

        def build_http_client(**kwargs):
            return SDKDefaultAsyncHttpxClient(
                **kwargs,
                transport=httpx.MockTransport(handle_request),
            )

        loop = asyncio.new_event_loop()
        try:
            with patch(
                "openai.DefaultAsyncHttpxClient",
                side_effect=build_http_client,
            ):
                loop.run_until_complete(
                    viewmodel.send_message.__wrapped__(viewmodel, "transport test")
                )
        finally:
            loop.close()

        assert request_urls == [
            "https://chat.example/v1/chat/completions"
            "?region_code=north&region_code=south&flag=&routing_key=primary"
        ]
        assert viewmodel.current_messages[-1] == {
            "role": "assistant",
            "content": "ok",
        }
        assert viewmodel.current_total_tokens == 3

    def test_send_message_exception(self, viewmodel, qtbot, caplog) -> None:
        """Provider 异常只向日志和 GUI 暴露固定安全消息。"""
        viewmodel.current_session_id = "test-id"
        sentinel = "chat-provider-response-secret"

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError(
                f"bad response https://example/v1?jwt={sentinel}: {sentinel}"
            )
        )

        loop = asyncio.new_event_loop()
        try:
            with patch(
                "openai.AsyncOpenAI",
                return_value=mock_client,
            ):
                with caplog.at_level(
                    logging.ERROR,
                    logger="pkv.gui.viewmodels.chat",
                ):
                    with qtbot.waitSignal(
                        viewmodel.chat_request_failed, timeout=1000
                    ) as blocker:
                        loop.run_until_complete(
                            viewmodel.send_message.__wrapped__(viewmodel, "test")
                        )
        finally:
            loop.close()

        assert blocker.args[2] == "chat_provider_failed"
        assert blocker.args[3] == (
            "发送消息失败，请检查 LLM Provider 配置或网络连接"
        )
        assert sentinel not in caplog.text
        assert sentinel not in blocker.args[3]


# ===================================================================
# _save_session (async)
# ===================================================================


class TestSaveSession:
    """保存会话测试（async）"""

    def test_save_success(self, viewmodel, mock_dependencies) -> None:
        """保存成功"""
        viewmodel.current_session_id = "test-id"
        viewmodel.current_messages = [{"role": "user", "content": "x"}]

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(viewmodel._save_session())
        finally:
            loop.close()

        mock_dependencies["store"].update_session.assert_called_once()

    def test_save_error_propagates(self, viewmodel, mock_dependencies) -> None:
        """保存异常必须传播，禁止上层误发 completed。"""
        viewmodel.current_session_id = "test-id"
        mock_dependencies["store"].update_session.side_effect = RuntimeError("err")

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(RuntimeError, match="err"):
                loop.run_until_complete(viewmodel._save_session())
        finally:
            loop.close()


# ===================================================================
# archive_url_and_inject (async)
# ===================================================================


class TestArchiveUrlAndInject:
    """URL 归档注入测试（async）"""

    def test_url_already_archived(self, viewmodel, mock_dependencies, qtbot) -> None:
        """URL 已归档时直接发射 completed"""
        mock_dependencies["store"].query_by_url.return_value = {
            "knowledge_id": 42,
            "title": "已有文章",
        }

        loop = asyncio.new_event_loop()
        try:
            with qtbot.waitSignal(viewmodel.url_archive_completed, timeout=1000):
                loop.run_until_complete(
                    viewmodel.archive_url_and_inject.__wrapped__(
                        viewmodel, "https://example.com"
                    )
                )
        finally:
            loop.close()

    def test_url_archive_exception(self, viewmodel, mock_dependencies, qtbot) -> None:
        """归档异常时发射 failed"""
        mock_dependencies["store"].query_by_url.return_value = None

        # Mock WorkflowEngine 抛异常
        with patch(
            "src.workflow.engine.WorkflowEngine",
            side_effect=RuntimeError("engine error"),
        ):
            loop = asyncio.new_event_loop()
            try:
                with qtbot.waitSignal(viewmodel.url_archive_failed, timeout=1000):
                    loop.run_until_complete(
                        viewmodel.archive_url_and_inject.__wrapped__(
                            viewmodel, "https://example.com/new"
                        )
                    )
            finally:
                loop.close()


# ===================================================================
# save_session_to_knowledge_base
# ===================================================================


class TestSaveSessionToKnowledgeBase:
    """保存对话到知识库的后台 worker 契约。"""

    def test_formats_messages_excludes_system_and_emits_success(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-1",
            "设计复盘",
            [
                {"role": "system", "content": "不得归档的隐藏上下文"},
                {"role": "user", "content": "问题一"},
                {"role": "assistant", "content": "回答一"},
            ],
        )
        worker = MagicMock()

        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
            return_value=worker,
        ) as worker_class:
            assert viewmodel.save_session_to_knowledge_base("session-1") is True

        worker_class.assert_called_once_with(
            mode="text",
            data={
                "title": "设计复盘",
                "text": (
                    "# 设计复盘\n\n"
                    "**User**:\n问题一\n\n"
                    "**Assistant**:\n回答一\n"
                ),
            },
        )
        worker.start.assert_called_once_with()
        success_callback = worker.finished_ok.connect.call_args.args[0]
        with qtbot.waitSignal(viewmodel.session_saved_to_kb, timeout=1000) as blocker:
            success_callback(_kb_success_payload(73))
        assert blocker.args == ["session-1", 73]

    def test_worker_error_is_forwarded_with_session_id(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-2",
            "失败样例",
            [{"role": "user", "content": "hello"}],
        )
        worker = MagicMock()

        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
            return_value=worker,
        ):
            assert viewmodel.save_session_to_knowledge_base("session-2") is True

        error_callback = worker.finished_err.connect.call_args.args[0]
        with qtbot.waitSignal(
            viewmodel.session_save_to_kb_failed, timeout=1000
        ) as blocker:
            error_callback("存储失败")
        assert blocker.args == [
            "session-2",
            "归档失败（错误代码：workflow_step_failed，阶段：workflow）",
        ]

    def test_degraded_save_emits_visible_warning(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-degraded",
            "降级样例",
            [{"role": "user", "content": "hello"}],
        )
        worker = MagicMock()
        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
            return_value=worker,
        ):
            assert viewmodel.save_session_to_knowledge_base("session-degraded")

        success_callback = worker.finished_ok.connect.call_args.args[0]
        with qtbot.waitSignal(
            viewmodel.session_save_to_kb_warning, timeout=1000
        ) as blocker:
            success_callback(
                _kb_success_payload(
                    88,
                    terminal="degraded",
                    status="degraded",
                )
            )
        assert blocker.args[0] == "session-degraded"
        assert "repair=rebuild_vectors_for_entry" in blocker.args[1]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            _kb_success_payload(0),
            _kb_success_payload(True),
            _kb_success_payload(73, terminal="error"),
            _kb_success_payload(73, core_committed=False),
            _kb_success_payload(73, terminal="success", status="degraded"),
            MappingProxyType(_kb_success_payload(73)),
        ],
        ids=[
            "empty",
            "zero-id",
            "bool-id",
            "error-terminal",
            "core-not-committed",
            "success-degraded-status",
            "mapping-subclass",
        ],
    )
    def test_malformed_worker_success_payload_is_fail_closed(
        self,
        viewmodel,
        mock_dependencies,
        payload,
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-invalid-success",
            "失败样例",
            [{"role": "user", "content": "hello"}],
        )
        worker = MagicMock()
        successes = []
        failures = []
        warnings = []
        viewmodel.session_saved_to_kb.connect(
            lambda *args: successes.append(tuple(args))
        )
        viewmodel.session_save_to_kb_failed.connect(
            lambda *args: failures.append(tuple(args))
        )
        viewmodel.session_save_to_kb_warning.connect(
            lambda *args: warnings.append(tuple(args))
        )

        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
            return_value=worker,
        ):
            assert viewmodel.save_session_to_knowledge_base(
                "session-invalid-success"
            )

        success_callback = worker.finished_ok.connect.call_args.args[0]
        success_callback(payload)

        assert successes == []
        assert warnings == []
        assert failures == [
            ("session-invalid-success", "保存对话到知识库失败")
        ]

    def test_structured_worker_failure_is_safe_and_not_duplicated(
        self, viewmodel, mock_dependencies
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-failure",
            "失败样例",
            [{"role": "user", "content": "hello"}],
        )
        worker = MagicMock()
        failures = []
        viewmodel.session_save_to_kb_failed.connect(
            lambda *args: failures.append(tuple(args))
        )
        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
            return_value=worker,
        ):
            assert viewmodel.save_session_to_knowledge_base("session-failure")

        structured_callback = worker.finished_failure.connect.call_args.args[0]
        legacy_callback = worker.finished_err.connect.call_args.args[0]
        structured_callback(
            {
                "safe_message": "归档失败（错误代码：workflow_step_failed）",
                "issues": [{"message": "raw-secret"}],
            }
        )
        legacy_callback("legacy duplicate")
        assert failures == [
            (
                "session-failure",
                "归档失败（错误代码：workflow_step_failed，阶段：workflow）",
            )
        ]

    @pytest.mark.parametrize(
        ("session", "expected_error"),
        [
            (None, "会话不存在"),
            (_kb_session_row("session-3", "空", []), "会话无对话内容"),
        ],
        ids=["missing-session", "empty-messages"],
    )
    def test_rejects_missing_or_empty_session(
        self,
        viewmodel,
        mock_dependencies,
        qtbot,
        session,
        expected_error,
    ) -> None:
        mock_dependencies["store"].get_session.return_value = session
        with qtbot.waitSignal(
            viewmodel.session_save_to_kb_failed, timeout=1000
        ) as blocker:
            result = viewmodel.save_session_to_knowledge_base("session-3")
        assert result is False
        assert blocker.args == ["session-3", expected_error]

    def test_rejects_malformed_message_json(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-4",
            "损坏会话",
            "{not-json",
        )
        with qtbot.waitSignal(
            viewmodel.session_save_to_kb_failed, timeout=1000
        ) as blocker:
            result = viewmodel.save_session_to_knowledge_base("session-4")
        assert result is False
        assert blocker.args[0] == "session-4"
        assert blocker.args[1] == "保存对话失败，请检查本地存储状态"

    def test_wrong_identity_or_custom_fields_never_construct_worker(
        self,
        viewmodel,
        mock_dependencies,
        qtbot,
    ) -> None:
        class StringCanary:
            calls = 0

            def __str__(self):
                self.calls += 1
                return "SAVE-KB-SECRET"

        title_canary = StringCanary()
        content_canary = StringCanary()
        corrupt_sessions = [
            _kb_session_row(
                "wrong-session",
                "wrong identity",
                [{"role": "user", "content": "hello"}],
            ),
            _kb_session_row(
                "session-strict",
                title_canary,
                [{"role": "user", "content": "hello"}],
            ),
            _kb_session_row(
                "session-strict",
                "bad content",
                [{"role": "user", "content": content_canary}],
            ),
        ]

        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
        ) as worker_class:
            for corrupt in corrupt_sessions:
                mock_dependencies["store"].get_session.return_value = corrupt
                with qtbot.waitSignal(
                    viewmodel.session_save_to_kb_failed,
                    timeout=1000,
                ) as blocker:
                    assert viewmodel.save_session_to_knowledge_base(
                        "session-strict"
                    ) is False
                assert blocker.args == [
                    "session-strict",
                    "保存对话失败，请检查本地存储状态",
                ]

        worker_class.assert_not_called()
        assert title_canary.calls == 0
        assert content_canary.calls == 0

    def test_second_save_is_rejected_while_worker_is_running(
        self,
        viewmodel,
        mock_dependencies,
        qtbot,
    ) -> None:
        mock_dependencies["store"].get_session.return_value = _kb_session_row(
            "session-busy",
            "busy",
            [{"role": "user", "content": "hello"}],
        )
        worker = MagicMock()
        worker.isRunning.return_value = True

        with patch(
            "src.gui.viewmodels.archive_viewmodel.ArchiveWorker",
            return_value=worker,
        ) as worker_class:
            assert viewmodel.save_session_to_knowledge_base("session-busy") is True
            with qtbot.waitSignal(
                viewmodel.session_save_to_kb_failed,
                timeout=1000,
            ) as blocker:
                assert viewmodel.save_session_to_knowledge_base(
                    "session-busy"
                ) is False

        assert blocker.args == [
            "session-busy",
            "已有保存任务正在进行，请稍后重试",
        ]
        worker_class.assert_called_once()
        assert mock_dependencies["store"].get_session.call_count == 1


class TestArchiveUrlAndInjectCompletion:
    """URL 归档工作流完成分支。"""

    def test_url_archive_success(self, viewmodel, mock_dependencies, qtbot) -> None:
        """归档成功"""
        mock_dependencies["store"].query_by_url.return_value = None
        mock_dependencies["store"].query_by_id.return_value = {
            "knowledge_id": 99,
            "title": "新文章",
        }

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.terminal = "success"
        mock_result.data = _url_completion_data(99)
        mock_result.errors = []
        mock_result.warnings = []
        mock_result.issues = []

        mock_engine = MagicMock()
        mock_engine.execute_async = AsyncMock(return_value=mock_result)

        with patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=mock_engine,
        ):
            loop = asyncio.new_event_loop()
            try:
                with qtbot.waitSignal(viewmodel.url_archive_completed, timeout=1000):
                    loop.run_until_complete(
                        viewmodel.archive_url_and_inject.__wrapped__(
                            viewmodel, "https://example.com/new"
                        )
                    )
            finally:
                loop.close()

    def test_ready_archive_does_not_emit_repair_warning(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        """READY is committed/non-retryable, but it does not need repair."""
        mock_dependencies["store"].query_by_url.return_value = None
        mock_dependencies["store"].query_by_id.return_value = {
            "knowledge_id": 100,
            "title": "Ready article",
        }
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.terminal = "success"
        mock_result.data = _url_completion_data(100)
        mock_result.errors = []
        mock_result.warnings = []
        mock_result.issues = []
        mock_engine = MagicMock()
        mock_engine.execute_async = AsyncMock(return_value=mock_result)
        warning_handler = MagicMock()
        viewmodel.url_archive_warning.connect(warning_handler)

        with patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=mock_engine,
        ):
            loop = asyncio.new_event_loop()
            try:
                with qtbot.waitSignal(viewmodel.url_archive_completed, timeout=1000):
                    loop.run_until_complete(
                        viewmodel.archive_url_and_inject.__wrapped__(
                            viewmodel, "https://example.com/ready"
                        )
                    )
            finally:
                loop.close()

        warning_handler.assert_not_called()

    def test_url_archive_workflow_failure(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        """WorkflowEngine 返回失败结果"""
        mock_dependencies["store"].query_by_url.return_value = None

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.terminal = "error"
        mock_result.data = {}
        mock_result.issues = []
        mock_result.errors = ["抓取失败"]

        mock_engine = MagicMock()
        mock_engine.execute_async = AsyncMock(return_value=mock_result)

        with patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=mock_engine,
        ):
            loop = asyncio.new_event_loop()
            try:
                with qtbot.waitSignal(viewmodel.url_archive_failed, timeout=1000):
                    loop.run_until_complete(
                        viewmodel.archive_url_and_inject.__wrapped__(
                            viewmodel, "https://example.com/fail"
                        )
                    )
            finally:
                loop.close()

    def test_workflow_degraded_is_visible_and_skip_review_is_set(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        mock_dependencies["store"].query_by_url.return_value = None
        mock_dependencies["store"].query_by_id.return_value = {
            "knowledge_id": 101,
            "title": "Degraded article",
        }
        mock_result = MagicMock(
            success=True,
            terminal="degraded",
            errors=[],
            warnings=["index warning"],
            issues=[],
            data=_url_completion_data(101),
        )
        mock_engine = MagicMock()
        mock_engine.execute_async = AsyncMock(return_value=mock_result)

        with patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=mock_engine,
        ):
            loop = asyncio.new_event_loop()
            try:
                with qtbot.waitSignal(
                    viewmodel.url_archive_warning, timeout=1000
                ) as blocker:
                    loop.run_until_complete(
                        viewmodel.archive_url_and_inject.__wrapped__(
                            viewmodel,
                            "https://example.com/degraded",
                        )
                    )
            finally:
                loop.close()

        assert "workflow_terminal=degraded" in blocker.args[1]
        assert mock_engine.execute_async.await_args.args[1]["skip_review"] is True

    def test_workflow_fatal_does_not_expose_raw_errors(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        sentinel = "workflow-provider-secret"
        mock_dependencies["store"].query_by_url.return_value = None
        mock_result = MagicMock(
            success=False,
            terminal="error",
            errors=[sentinel],
            warnings=[],
            issues=[
                {
                    "code": "workflow_step_failed",
                    "stage": "fetch",
                }
            ],
            data={},
        )
        mock_engine = MagicMock()
        mock_engine.execute_async = AsyncMock(return_value=mock_result)

        with patch(
            "src.workflow.engine.WorkflowEngine",
            return_value=mock_engine,
        ):
            loop = asyncio.new_event_loop()
            try:
                with qtbot.waitSignal(
                    viewmodel.url_archive_failed, timeout=1000
                ) as blocker:
                    loop.run_until_complete(
                        viewmodel.archive_url_and_inject.__wrapped__(
                            viewmodel,
                            "https://example.com/fatal",
                        )
                    )
            finally:
                loop.close()

        assert blocker.args[1] == (
            "归档失败（错误代码：workflow_step_failed，阶段：fetch）"
        )
        assert sentinel not in blocker.args[1]
