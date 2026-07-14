"""AI 对话 ViewModel (M12)

提供 AI 对话功能的 ViewModel 层，使用 qasync 实现异步流式输出。

核心特性：
- 流式输出：使用 OpenAI SDK 的 stream=True，实时发射 token
- Token 统计：stream_options={"include_usage": True} 获取精确统计
- 会话管理：多会话切换、新建、归档
- 数据持久化：自动保存到 SQLiteStore
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal
from qasync import asyncSlot

from src.storage.sqlite_store import SQLiteStore
from src.utils.config import Config

logger = logging.getLogger("pkv.gui.viewmodels.chat")


class ChatViewModel(QObject):
    """AI 对话 ViewModel

    负责管理 AI 对话会话，处理流式输出，发射 Signal 更新 UI。

    Signals:
        token_received(str): 收到新 token（用于流式渲染）
        token_usage_updated(int, int, int): Token 使用更新（input, output, total）
        stream_finished(): 流式输出完成
        stream_stopped(): 用户手动停止
        error_occurred(str): 错误发生
        session_created(str, str): 新会话创建（session_id, title）
        session_loaded(str): 会话加载完成（session_id）
        url_archive_started(str): URL 归档开始（url）
        url_archive_completed(str, dict): URL 归档完成（url, entry_dict）
        url_archive_failed(str, str): URL 归档失败（url, error_msg）
    """

    token_received = Signal(str)  # token 内容
    token_usage_updated = Signal(int, int, int)  # input_tokens, output_tokens, total_tokens
    stream_finished = Signal()
    stream_stopped = Signal()
    error_occurred = Signal(str)  # 错误消息
    session_created = Signal(str, str)  # session_id, title
    session_loaded = Signal(str)  # session_id
    # M12 Phase 3: URL 归档信号
    url_archive_started = Signal(str)
    url_archive_completed = Signal(str, dict)
    url_archive_failed = Signal(str, str)

    # 对话保存到知识库
    session_saved_to_kb = Signal(str, int)  # session_id, knowledge_id
    session_save_to_kb_failed = Signal(str, str)  # session_id, error_msg

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """初始化 ChatViewModel

        Args:
            parent: Qt 父对象
        """
        super().__init__(parent)

        # 配置
        self.config = Config()
        self.db_path = self.config.get("storage.db_path")
        self.store = SQLiteStore(self.db_path)

        # 当前会话状态
        self.current_session_id: Optional[str] = None
        self.current_messages: List[dict] = []
        self.current_total_tokens: int = 0
        self.current_round_count: int = 0

        # OpenAI-compatible LLM 客户端配置
        self.api_key = self.config.llm_api_key
        self.base_url = self.config.llm_base_url
        self.model = self.config.llm_model
        self.max_tokens = 2000  # 单次响应最大 token 数

        # 停止标志
        self._stop_flag = False

        logger.info("ChatViewModel 初始化完成")

    def create_new_session(self, title: Optional[str] = None) -> str:
        """创建新会话

        Args:
            title: 会话标题（可选，默认为"新对话"）

        Returns:
            新会话的 session_id
        """
        try:
            session_id = str(uuid.uuid4())
            session_title = title or "新对话"

            # 创建数据库记录
            self.store.create_session(session_id, session_title)

            # 重置当前会话状态
            self.current_session_id = session_id
            self.current_messages = []
            self.current_total_tokens = 0
            self.current_round_count = 0

            logger.info(f"✅ 创建新会话: {session_id} - {session_title}")
            self.session_created.emit(session_id, session_title)
            return session_id

        except Exception as e:
            error_msg = f"创建会话失败: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            raise

    def load_session(self, session_id: str) -> bool:
        """加载已有会话

        Args:
            session_id: 会话 ID

        Returns:
            是否加载成功
        """
        try:
            session = self.store.get_session(session_id)
            if not session:
                error_msg = f"会话不存在: {session_id}"
                logger.warning(error_msg)
                self.error_occurred.emit(error_msg)
                return False

            # 恢复会话状态
            self.current_session_id = session_id
            self.current_messages = session["messages"]
            self.current_total_tokens = session["total_tokens"]
            self.current_round_count = session["round_count"]

            logger.info(
                f"✅ 加载会话: {session_id} (轮数: {self.current_round_count}, "
                f"Tokens: {self.current_total_tokens})"
            )
            self.session_loaded.emit(session_id)
            return True

        except Exception as e:
            error_msg = f"加载会话失败: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False

    def list_sessions(self, is_archived: bool = False) -> List[Dict[str, Any]]:
        """列出会话

        Args:
            is_archived: 是否只显示归档会话

        Returns:
            会话列表
        """
        try:
            return self.store.list_sessions(is_archived=is_archived)
        except Exception as e:
            error_msg = f"列出会话失败: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return []

    def set_knowledge_context(self, context_text: str) -> None:
        """设置知识引用上下文（作为 system message 注入）

        在消息列表开头插入 system message，作为 AI 回答的参考上下文。
        每次调用会替换已有的 system message。

        Args:
            context_text: 格式化的知识引用上下文
        """
        if not context_text:
            return

        # 移除已有的 system message（如果有）
        self.current_messages = [
            msg for msg in self.current_messages
            if msg.get("role") != "system"
        ]

        # 在列表开头插入 system message
        self.current_messages.insert(0, {
            "role": "system",
            "content": context_text,
        })

        logger.info(
            f"📎 已设置知识引用上下文 "
            f"(~{len(context_text)} chars)"
        )

    def get_current_messages(self) -> List[dict]:
        """获取当前会话消息

        Returns:
            消息列表（OpenAI 格式）
        """
        return self.current_messages

    def get_token_stats(self) -> Dict[str, int]:
        """获取 Token 统计

        Returns:
            统计字典，包含 total_tokens, round_count
        """
        return {
            "total_tokens": self.current_total_tokens,
            "round_count": self.current_round_count,
        }

    @asyncSlot()
    async def send_message(self, user_message: str) -> None:
        """发送消息并处理流式响应

        Args:
            user_message: 用户消息内容

        注意：
            - 使用 @asyncSlot() 装饰器，在 qasync 事件循环中执行
            - 发射 token_received Signal 实现流式输出
            - 发射 token_usage_updated Signal 更新 Token 统计
        """
        if not self.current_session_id:
            self.error_occurred.emit("未选择会话，请先创建或加载会话")
            return

        if not self.api_key:
            self.error_occurred.emit("未配置 PKV_LLM_API_KEY，请检查 .env 文件")
            return

        try:
            # 1. 添加 user message
            self.current_messages.append({
                "role": "user",
                "content": user_message
            })
            self.current_round_count += 1

            # 2. 初始化 OpenAI 客户端
            try:
                from openai import AsyncOpenAI
            except ImportError:
                self.error_occurred.emit("未安装 openai 库，请运行 pip install openai")
                return

            client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url
            )

            # 3. 调用流式 API
            self._stop_flag = False
            assistant_content = ""
            input_tokens = 0
            output_tokens = 0

            logger.info(f"📤 发送消息到 DeepSeek API (轮数: {self.current_round_count})")

            stream = await client.chat.completions.create(
                model=self.model,
                messages=self.current_messages,
                stream=True,
                stream_options={"include_usage": True},  # 启用 Token 统计
                max_tokens=self.max_tokens,
            )
            try:
                async for chunk in stream:
                    # 检查停止标志
                    if self._stop_flag:
                        logger.info("⏹ 用户停止流式输出")
                        self.stream_stopped.emit()
                        break

                    # 提取 delta content
                    if chunk.choices:
                        delta = chunk.choices[0].delta
                        if delta.content:
                            token = delta.content
                            assistant_content += token
                            self.token_received.emit(token)  # 发射 token Signal

                    # 提取 usage 信息（仅最后一个 chunk 包含）
                    if hasattr(chunk, 'usage') and chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens
                        output_tokens = chunk.usage.completion_tokens
            finally:
                await stream.close()

            # 4. 添加 assistant message
            if assistant_content:  # 只有在有内容时才添加
                self.current_messages.append({
                    "role": "assistant",
                    "content": assistant_content
                })

            # 5. 更新 Token 统计
            self.current_total_tokens += (input_tokens + output_tokens)
            self.token_usage_updated.emit(
                input_tokens,
                output_tokens,
                self.current_total_tokens
            )

            logger.info(
                f"✅ 流式响应完成 (Input: {input_tokens}, Output: {output_tokens}, "
                f"Total: {self.current_total_tokens})"
            )

            # 6. 保存到数据库
            if not self._stop_flag:  # 只在正常完成时保存
                await self._save_session()
                self.stream_finished.emit()

        except Exception as e:
            error_msg = f"发送消息失败: {e}"
            logger.error(error_msg, exc_info=True)
            self.error_occurred.emit(error_msg)

    async def _save_session(self) -> None:
        """保存当前会话到数据库（内部方法）"""
        try:
            self.store.update_session(
                session_id=self.current_session_id,
                messages=self.current_messages,
                total_tokens=self.current_total_tokens,
                round_count=self.current_round_count,
                summary=None  # TODO: 后续可添加 AI 生成摘要
            )
            logger.debug(f"💾 会话已保存: {self.current_session_id}")
        except Exception as e:
            logger.error(f"保存会话失败: {e}")
            # 不抛出异常，避免影响主流程

    def stop_stream(self) -> None:
        """停止流式输出"""
        self._stop_flag = True
        logger.info("🛑 设置停止标志")

    def delete_current_session(self) -> bool:
        """删除当前会话

        Returns:
            是否删除成功
        """
        if not self.current_session_id:
            return False
        return self.delete_session(self.current_session_id)

    def delete_session(self, session_id: str) -> bool:
        """删除指定会话

        Args:
            session_id: 要删除的会话 ID

        Returns:
            是否删除成功
        """
        try:
            self.store.delete_session(session_id)
            logger.info(f"🗑️ 删除会话: {session_id}")

            # 如果删除的是当前会话，重置状态
            if session_id == self.current_session_id:
                self.current_session_id = None
                self.current_messages = []
                self.current_total_tokens = 0
                self.current_round_count = 0

            return True
        except Exception as e:
            error_msg = f"删除会话失败: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False

    def archive_current_session(self) -> bool:
        """归档当前会话

        Returns:
            是否归档成功
        """
        if not self.current_session_id:
            return False

        try:
            self.store.archive_session(self.current_session_id, is_archived=True)
            logger.info(f"📦 归档会话: {self.current_session_id}")
            return True
        except Exception as e:
            error_msg = f"归档会话失败: {e}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False

    # ------------------------------------------------------------------
    # M12 Phase 3: URL 自动检测和归档
    # ------------------------------------------------------------------

    @asyncSlot()
    async def archive_url_and_inject(self, url: str) -> None:
        """归档 URL 并将内容注入对话上下文

        使用 WorkflowEngine 归档 URL，成功后：
        1. 构建知识引用
        2. 注入到当前会话上下文
        3. 发射完成信号

        Args:
            url: 要归档的 URL
        """
        self.url_archive_started.emit(url)
        logger.info(f"🔗 开始归档 URL: {url}")

        try:
            # 先检查 URL 是否已归档
            existing = self.store.query_by_url(url)
            if existing:
                logger.info(f"📎 URL 已归档，直接引用: {url}")
                self.url_archive_completed.emit(url, existing)
                return

            # 使用 WorkflowEngine 归档
            from src.workflow.engine import WorkflowEngine
            engine = WorkflowEngine()

            result = await engine.execute_async(
                "archive-url",
                {
                    "url": url,
                    "skip_sharpen": True,  # GUI 不触发交互
                },
            )

            if result.success:
                knowledge_id = result.data.get("knowledge_id")
                if knowledge_id:
                    entry = self.store.query_by_id(knowledge_id)
                    if entry:
                        logger.info(
                            f"✅ URL 归档成功: {url} → ID {knowledge_id}"
                        )
                        self.url_archive_completed.emit(url, entry)
                        return

                # 归档成功但无法获取条目
                self.url_archive_completed.emit(url, result.data)
            else:
                error_msg = "; ".join(result.errors) if result.errors else "未知错误"
                logger.warning(f"⚠️ URL 归档失败: {url} - {error_msg}")
                self.url_archive_failed.emit(url, error_msg)

        except Exception as e:
            error_msg = f"归档异常: {e}"
            logger.error(f"❌ URL 归档异常: {url} - {e}", exc_info=True)
            self.url_archive_failed.emit(url, error_msg)

    # ------------------------------------------------------------------
    # 对话保存到知识库
    # ------------------------------------------------------------------

    def save_session_to_knowledge_base(self, session_id: str) -> bool:
        """将对话内容保存到知识库

        将指定会话的完整对话记录格式化为文本，使用 TextFallback 工作流归档。

        Args:
            session_id: 会话 ID

        Returns:
            是否成功触发归档（归档本身在后台线程执行）
        """
        try:
            session = self.store.get_session(session_id)
            if not session:
                self.session_save_to_kb_failed.emit(session_id, "会话不存在")
                return False

            raw = session.get("messages", [])
            # get_session 可能返回已解析的 list 或 JSON 字符串
            if isinstance(raw, str):
                messages = json.loads(raw) if raw else []
            else:
                messages = raw or []

            if not messages:
                self.session_save_to_kb_failed.emit(session_id, "会话无对话内容")
                return False

            # 格式化对话记录为文本
            title = session.get("title", "AI 对话记录")
            lines = [f"# {title}\n"]
            for msg in messages:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role == "system":
                    continue  # 跳过 system 消息
                label = "**User**" if role == "user" else "**Assistant**"
                lines.append(f"{label}:\n{content}\n")

            text_content = "\n".join(lines)

            # 使用 ArchiveWorker 在后台线程归档
            from src.gui.viewmodels.archive_viewmodel import ArchiveWorker

            self._kb_worker = ArchiveWorker(
                mode="text",
                data={"text": text_content, "title": title},
            )
            self._kb_worker.finished_ok.connect(
                lambda result: self._on_session_saved_to_kb(
                    session_id, result
                )
            )
            self._kb_worker.finished_err.connect(
                lambda err: self.session_save_to_kb_failed.emit(
                    session_id, err
                )
            )
            self._kb_worker.start()

            logger.info(f"📥 开始保存对话到知识库: {session_id}")
            return True

        except Exception as e:
            error_msg = f"保存对话失败: {e}"
            logger.error(error_msg, exc_info=True)
            self.session_save_to_kb_failed.emit(session_id, error_msg)
            return False

    def _on_session_saved_to_kb(
        self,
        session_id: str,
        result: dict,
    ) -> None:
        """对话保存到知识库成功回调

        Args:
            session_id: 会话 ID
            result: 归档结果字典
        """
        knowledge_id = result.get("knowledge_id", 0)
        logger.info(
            f"✅ 对话已保存到知识库: {session_id} → ID {knowledge_id}"
        )
        self.session_saved_to_kb.emit(session_id, knowledge_id)
