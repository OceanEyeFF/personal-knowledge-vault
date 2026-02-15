"""
工作流步骤实现

包含抓取、分析、idea sharpen、存储等核心步骤。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from src.ai.deepseek_client import DeepSeekClient
from src.ai.embedder import Embedder
from src.processors import get_processor
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.utils.config import get_config
from src.utils.text_utils import TextProcessor
from src.workflow.models import WorkflowContext


class BaseStep(ABC):
    """工作流步骤基类。"""

    def __init__(self, step_id: str, config: Dict[str, Any]) -> None:
        """
        初始化步骤。

        Args:
            step_id: 步骤 ID
            config: 步骤配置
        """
        self.step_id = step_id
        self.config = config

    @abstractmethod
    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        执行步骤逻辑。

        Args:
            context: 工作流上下文

        Returns:
            结果字典（将合并到上下文 state）
        """

    def _log(self, context: WorkflowContext, message: str) -> None:
        """
        记录步骤日志。

        Args:
            context: 工作流上下文
            message: 日志内容
        """
        context.log(f"[{self.step_id}] {message}")


class FetchStep(BaseStep):
    """内容抓取步骤（集成 processors）。"""

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        抓取内容并返回 Entry。

        Args:
            context: 工作流上下文

        Returns:
            包含 entry 与基础字段的字典
        """
        url_key = self.config.get("url_key", "url")
        url = context.state.get(url_key) or context.state.get("url")
        if not url:
            message = f"缺少 URL 输入: {url_key}"
            self._log(context, message)
            return {"errors": [message]}

        processor_name = self.config.get("processor", "auto")
        retry_times = int(self.config.get("retry", 0))

        last_error: Optional[str] = None
        for attempt in range(retry_times + 1):
            try:
                if processor_name != "auto":
                    self._log(context, f"使用指定处理器: {processor_name}")
                processor = get_processor(url)
                entry = await processor.process(url)
                self._log(context, f"抓取完成: title={entry.title}")
                return {
                    "entry": entry,
                    "content": entry.content,
                    "title": entry.title,
                    "source_type": entry.source_type,
                    "source_url": entry.source_url,
                }
            except Exception as e:
                last_error = f"抓取失败: {e}"
                self._log(context, f"{last_error} (第 {attempt + 1}/{retry_times + 1} 次)")
                if attempt < retry_times:
                    await asyncio.sleep(1)

        return {"errors": [last_error] if last_error else ["抓取失败"]}


class AnalyzeStep(BaseStep):
    """AI 分析步骤（集成 AI 服务）。"""

    def __init__(
        self,
        step_id: str,
        config: Dict[str, Any],
        deepseek_client: Optional[DeepSeekClient] = None,
    ) -> None:
        """
        初始化分析步骤。

        Args:
            step_id: 步骤 ID
            config: 步骤配置
            deepseek_client: 可注入的 DeepSeek 客户端（用于测试）
        """
        super().__init__(step_id, config)
        self._client = deepseek_client

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        执行 AI 分析。

        Args:
            context: 工作流上下文

        Returns:
            分析结果字典
        """
        entry: Optional[Entry] = context.state.get("entry")
        content = context.state.get("content") or (entry.content if entry else "")
        if not content:
            message = "内容为空，跳过分析"
            self._log(context, message)
            return {"errors": [message]}

        tasks = self.config.get("tasks") or ["summarize", "extract_tags"]
        summary: Optional[str] = None
        tags: List[str] = []
        errors: List[str] = []

        config = get_config()
        model = config.get("ai.deepseek.model", "deepseek-chat")
        max_words = int(self.config.get("max_words", 300))
        num_tags = int(self.config.get("num_tags", 5))

        client = self._client or DeepSeekClient(model=model)

        if "summarize" in tasks:
            try:
                summary = await asyncio.to_thread(client.summarize, content, max_words)
            except Exception as e:
                errors.append(f"摘要生成失败: {e}")

        if "extract_tags" in tasks:
            try:
                tags = await asyncio.to_thread(client.extract_tags, content, num_tags)
            except Exception as e:
                errors.append(f"标签提取失败: {e}")

        if "extract_concepts" in tasks:
            self._log(context, "暂不支持概念提取，已跳过")

        if entry:
            if summary:
                entry.summary_100_words = summary
                entry.summary_one_sentence = self._extract_first_sentence(summary)
                entry.abstract = summary
            if tags:
                entry.tags = tags

        result: Dict[str, Any] = {
            "summary": summary,
            "tags": tags,
            "content_length": len(content),
            "tag_count": len(tags),
        }

        if entry:
            result["entry"] = entry

        if errors:
            self._log(context, "部分分析失败，已降级")
            result["errors"] = errors

        return result

    @staticmethod
    def _extract_first_sentence(summary: str) -> str:
        """
        提取摘要的第一句话。

        Args:
            summary: 摘要内容

        Returns:
            一句话摘要
        """
        if not summary:
            return ""
        for delimiter in ["。", ".", "！", "!", "？", "?"]:
            if delimiter in summary:
                return summary.split(delimiter)[0].strip()
        return summary.strip()


class IdeaSharpenStep(BaseStep):
    """Idea Sharpen 步骤（人机协作）。"""

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        通过 CLI 交互采集用户输入。

        Args:
            context: 工作流上下文

        Returns:
            用户回答结果字典
        """
        if not self._should_run(context):
            self._log(context, "条件不满足，跳过 Idea Sharpen")
            return {}

        questions = self.config.get("questions") or []
        if not questions:
            self._log(context, "未配置问题，跳过 Idea Sharpen")
            return {}

        timeout = int(self.config.get("timeout", 300))
        skip_on_timeout = bool(self.config.get("skip_on_timeout", True))

        console = Console()
        answers: Dict[str, str] = {}

        for question in questions:
            console.print(Panel(question, title="Idea Sharpen"))
            try:
                answer = await asyncio.wait_for(
                    asyncio.to_thread(Prompt.ask, "你的回答"),
                    timeout=timeout,
                )
                answers[question] = answer
            except asyncio.TimeoutError:
                message = "用户响应超时，已跳过 Idea Sharpen"
                self._log(context, message)
                if skip_on_timeout:
                    return {"idea_sharpen": answers}
                raise

        entry: Optional[Entry] = context.state.get("entry")
        if entry and answers:
            notes = "\n".join([f"{q}: {a}" for q, a in answers.items()])
            entry.notes = f"{entry.notes}\n{notes}".strip() if entry.notes else notes

        return {"idea_sharpen": answers, "entry": entry}

    def _should_run(self, context: WorkflowContext) -> bool:
        """
        判断是否满足执行条件。

        Args:
            context: 工作流上下文

        Returns:
            是否执行
        """
        condition = self.config.get("condition")
        if not condition:
            return True

        entry: Optional[Entry] = context.state.get("entry")
        content = context.state.get("content") or (entry.content if entry else "")
        tags = context.state.get("tags") or (entry.tags if entry else [])
        concepts = context.state.get("concepts") or []

        variables = {
            "content_length": context.state.get("content_length", len(content)),
            "tag_count": context.state.get("tag_count", len(tags)),
            "concept_count": context.state.get("concept_count", len(concepts)),
            "content_type": context.state.get("content_type", entry.source_type if entry else ""),
            "source": context.state.get("source", entry.source_url if entry else ""),
        }

        try:
            return bool(eval(condition, {"__builtins__": {}}, variables))
        except Exception as e:
            self._log(context, f"条件解析失败: {e}")
            return False


class StoreStep(BaseStep):
    """存储步骤（集成 storage）。"""

    def __init__(
        self,
        step_id: str,
        config: Dict[str, Any],
        markdown_store: Optional[MarkdownStore] = None,
        sqlite_store: Optional[SQLiteStore] = None,
        vector_store: Optional[VectorStore] = None,
        embedder: Optional[Embedder] = None,
    ) -> None:
        """
        初始化存储步骤。

        Args:
            step_id: 步骤 ID
            config: 步骤配置
            markdown_store: 可注入的 MarkdownStore
            sqlite_store: 可注入的 SQLiteStore
            vector_store: 可注入的 VectorStore
            embedder: 可注入的 Embedder
        """
        super().__init__(step_id, config)
        self._markdown_store = markdown_store
        self._sqlite_store = sqlite_store
        self._vector_store = vector_store
        self._embedder = embedder

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        执行存储逻辑。

        Args:
            context: 工作流上下文

        Returns:
            存储结果字典
        """
        entry: Optional[Entry] = context.state.get("entry")
        if entry is None:
            message = "缺少 Entry，无法存储"
            self._log(context, message)
            return {"errors": [message]}

        targets = self.config.get("targets") or ["markdown"]
        config = get_config()

        file_path: Optional[str] = None
        knowledge_id: Optional[int] = None
        errors: List[str] = []

        if "markdown" in targets:
            try:
                markdown_store = self._markdown_store or MarkdownStore(config.vault_dir)
                saved_path = markdown_store.save(entry)
                file_path = str(saved_path)
            except Exception as e:
                errors.append(f"Markdown 存储失败: {e}")

        if "sqlite" in targets:
            try:
                sqlite_store = self._sqlite_store or SQLiteStore(config.db_path)
                sqlite_store.initialize()
                if not file_path:
                    safe_title = TextProcessor.sanitize_filename(entry.title or "entry")
                    file_path = str(config.vault_dir / f"{safe_title}.md")
                if isinstance(entry.keywords, list):
                    entry.keywords = ",".join(entry.keywords)
                knowledge_id = sqlite_store.insert_entry(entry, file_path=file_path)
            except Exception as e:
                errors.append(f"SQLite 存储失败: {e}")

        if "vector_index" in targets:
            if knowledge_id is None:
                errors.append("缺少 knowledge_id，跳过向量索引")
            else:
                try:
                    vector_store = self._vector_store or VectorStore(
                        index_dir=config.vector_index_dir,
                        dim=config.get("ai.openai.embedding_dim", 1536),
                    )
                    embedder = self._embedder or Embedder()
                    doc_vector = await asyncio.to_thread(embedder.embed_document, entry.content)
                    vector_store.add_doc_vector(knowledge_id, doc_vector)

                    chunk_vectors, _ = await asyncio.to_thread(
                        embedder.embed_chunks, entry.content, True
                    )
                    for idx, vector in enumerate(chunk_vectors):
                        vector_store.add_chunk_vector(knowledge_id, idx, vector)
                except Exception as e:
                    errors.append(f"向量索引失败: {e}")

        if errors:
            self._log(context, "部分存储失败，已降级")

        result: Dict[str, Any] = {
            "file_path": file_path,
            "knowledge_id": knowledge_id,
            "stored_targets": targets,
            "entry": entry,
        }
        if errors:
            result["errors"] = errors
        return result
