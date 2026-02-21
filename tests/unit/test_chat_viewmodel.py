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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Mock 所有外部依赖，避免真实初始化
@pytest.fixture(autouse=True)
def mock_dependencies(monkeypatch):
    """Mock Config 和 SQLiteStore，避免真实 DB 初始化"""
    mock_config = MagicMock()
    mock_config.get.return_value = ":memory:"
    mock_config.get_env.return_value = "fake-api-key"

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

        async def mock_stream_iter():
            for chunk in [mock_chunk1, mock_chunk2, mock_chunk3]:
                yield chunk

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream_iter())
        mock_stream.__aexit__ = AsyncMock(return_value=False)

        # 因为 mock_stream_iter() 返回的是 async generator，
        # 需要让 async with 返回的对象支持 async for
        mock_client = MagicMock()
        mock_client.chat.completions.create = MagicMock(return_value=mock_stream)

        received_tokens = []

        def on_token(t):
            received_tokens.append(t)

        viewmodel.token_received.connect(on_token)

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

    def test_send_message_exception(self, viewmodel, qtbot) -> None:
        """发送消息异常时发射 error_occurred"""
        viewmodel.current_session_id = "test-id"
        viewmodel.api_key = "fake-key"

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("API error")

        loop = asyncio.new_event_loop()
        try:
            with patch(
                "openai.AsyncOpenAI",
                return_value=mock_client,
            ):
                with qtbot.waitSignal(viewmodel.error_occurred, timeout=1000):
                    loop.run_until_complete(
                        viewmodel.send_message.__wrapped__(viewmodel, "test")
                    )
        finally:
            loop.close()


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
