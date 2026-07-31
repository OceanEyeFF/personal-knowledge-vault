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
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from openai import DefaultAsyncHttpxClient as SDKDefaultAsyncHttpxClient


# Mock 所有外部依赖，避免真实初始化
@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    """Mock Config 和 SQLiteStore，避免真实 DB 初始化"""
    mock_config = MagicMock()
    mock_config.db_path = ".data-test/db/runtime.db"
    mock_config.get.return_value = ":memory:"
    mock_config.llm_api_key = "fake-api-key"
    mock_config.llm_base_url = "https://llm.example/v1"
    mock_config.llm_model = "configured-model"

    mock_store = MagicMock()
    mock_store.create_session.return_value = None
    mock_store.get_session.return_value = None
    mock_store.list_sessions.return_value = []
    mock_store.update_session.return_value = None
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


def test_reload_provider_config_updates_cached_values(
    viewmodel, mock_dependencies
) -> None:
    """设置保存后应刷新下一次对话请求使用的 Provider 配置。"""
    config = mock_dependencies["config"]
    config.llm_api_key = "updated-key"
    config.llm_base_url = "https://updated.example.com/v1"
    config.llm_model = "updated-model"

    viewmodel.reload_provider_config()

    assert viewmodel.api_key == "updated-key"
    assert viewmodel.base_url == "https://updated.example.com/v1"
    assert viewmodel.model == "updated-model"


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
            {"session_id": "s1", "title": "对话1"},
            {"session_id": "s2", "title": "对话2"},
        ]
        result = viewmodel.list_sessions()
        assert len(result) == 2

    def test_list_archived(self, viewmodel, mock_dependencies) -> None:
        """列出归档会话"""
        viewmodel.list_sessions(is_archived=True)
        mock_dependencies["store"].list_sessions.assert_called_with(is_archived=True)

    def test_list_sessions_error(self, viewmodel, mock_dependencies) -> None:
        """列出会话异常返回空列表"""
        mock_dependencies["store"].list_sessions.side_effect = RuntimeError("DB error")
        result = viewmodel.list_sessions()
        assert result == []


# ===================================================================
# stop_stream
# ===================================================================


class TestStopStream:
    """停止流式输出测试"""

    def test_sets_stop_flag(self, viewmodel) -> None:
        """设置停止标志"""
        assert viewmodel._stop_flag is False
        viewmodel.stop_stream()
        assert viewmodel._stop_flag is True


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
        """无当前会话时发射 error_occurred"""
        viewmodel.current_session_id = None

        # 直接调用内部 async 逻辑
        loop = asyncio.new_event_loop()
        try:
            # send_message 是 @asyncSlot 装饰的，直接调用其 coro
            with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000):
                loop.run_until_complete(
                    viewmodel.send_message.__wrapped__(viewmodel, "hello")
                )
        finally:
            loop.close()

    def test_send_without_api_key(self, viewmodel, qtbot) -> None:
        """无 API Key 时发射 error_occurred"""
        viewmodel.current_session_id = "test-id"
        viewmodel.api_key = None

        loop = asyncio.new_event_loop()
        try:
            with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000):
                loop.run_until_complete(
                    viewmodel.send_message.__wrapped__(viewmodel, "hello")
                )
        finally:
            loop.close()

    def test_send_message_stream_success(self, viewmodel, mock_dependencies) -> None:
        """流式发送成功"""
        viewmodel.current_session_id = "test-id"
        viewmodel.api_key = "fake-key"

        # 构造 Mock stream
        mock_chunk1 = MagicMock()
        mock_chunk1.choices = [MagicMock()]
        mock_chunk1.choices[0].delta.content = "Hello"
        mock_chunk1.usage = None

        mock_chunk2 = MagicMock()
        mock_chunk2.choices = [MagicMock()]
        mock_chunk2.choices[0].delta.content = " World"
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
        viewmodel.api_key = "fake-key"
        viewmodel.model = "configured-model"
        viewmodel.base_url = (
            "https://chat.example/v1?region_code=north&region_code=south"
            "&flag=&routing_key=primary#client-only"
        )
        request_urls = []
        event_stream = (
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk",'
            '"created":1,"model":"configured-model","choices":['
            '{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
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
        viewmodel.api_key = "fake-key"
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
                        viewmodel.error_occurred, timeout=1000
                    ) as blocker:
                        loop.run_until_complete(
                            viewmodel.send_message.__wrapped__(viewmodel, "test")
                        )
        finally:
            loop.close()

        assert blocker.args == [
            "发送消息失败，请检查 LLM Provider 配置或网络连接"
        ]
        assert sentinel not in caplog.text
        assert sentinel not in blocker.args[0]


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

    def test_save_error_no_raise(self, viewmodel, mock_dependencies) -> None:
        """保存异常不抛出"""
        viewmodel.current_session_id = "test-id"
        mock_dependencies["store"].update_session.side_effect = RuntimeError("err")

        loop = asyncio.new_event_loop()
        try:
            # 不应抛异常
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
        mock_dependencies["store"].get_session.return_value = {
            "title": "设计复盘",
            "messages": [
                {"role": "system", "content": "不得归档的隐藏上下文"},
                {"role": "user", "content": "问题一"},
                {"role": "assistant", "content": "回答一"},
            ],
        }
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
            success_callback({"knowledge_id": 73})
        assert blocker.args == ["session-1", 73]

    def test_worker_error_is_forwarded_with_session_id(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        mock_dependencies["store"].get_session.return_value = {
            "title": "失败样例",
            "messages": [{"role": "user", "content": "hello"}],
        }
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
        assert blocker.args == ["session-2", "存储失败"]

    @pytest.mark.parametrize(
        ("session", "expected_error"),
        [
            (None, "会话不存在"),
            ({"title": "空", "messages": []}, "会话无对话内容"),
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
        mock_dependencies["store"].get_session.return_value = {
            "title": "损坏会话",
            "messages": "{not-json",
        }
        with qtbot.waitSignal(
            viewmodel.session_save_to_kb_failed, timeout=1000
        ) as blocker:
            result = viewmodel.save_session_to_knowledge_base("session-4")
        assert result is False
        assert blocker.args[0] == "session-4"
        assert blocker.args[1].startswith("保存对话失败:")

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
        mock_result.data = {"knowledge_id": 99}

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

    def test_url_archive_workflow_failure(
        self, viewmodel, mock_dependencies, qtbot
    ) -> None:
        """WorkflowEngine 返回失败结果"""
        mock_dependencies["store"].query_by_url.return_value = None

        mock_result = MagicMock()
        mock_result.success = False
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
