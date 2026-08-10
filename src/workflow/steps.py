"""
工作流步骤实现

包含抓取、分析、idea sharpen、存储等核心步骤。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from src.ai.deepseek_client import DeepSeekClient
from src.ai.embedder import Embedder
from src.processors import get_processor, get_processor_by_name, normalize_processor_name
from src.relations.citations import sanitize_public_source_url
from src.runtime.errors import ErrorCode, OperationStatus, PKVRuntimeError
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.coordinator import StorageCoordinator
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.utils.config import get_config
from src.utils.logger import get_logger
from src.workflow.config_schema import (
    evaluate_condition,
    evaluate_trigger_rules,
    validate_trigger_rules,
)
from src.workflow.models import WorkflowContext

logger = get_logger(__name__)


_CLI_LOCAL_FILE_IMPORT_KEY = "_pkv_cli_local_file_import_capability"
_CLI_LOCAL_FILE_IMPORT_TOKEN = object()


def _grant_cli_local_file_import(input_data: Dict[str, Any], source: str) -> None:
    """Attach a path-bound, non-serializable capability for the CLI adapter.

    The capability is intentionally private and identity checked.  A remote or
    serialized caller that supplies the same key with ``True`` or a string
    cannot authorize a local file read.
    """

    input_data[_CLI_LOCAL_FILE_IMPORT_KEY] = (
        _CLI_LOCAL_FILE_IMPORT_TOKEN,
        source,
    )


def _consume_cli_local_file_import(context: WorkflowContext, source: str) -> bool:
    """Consume the one-shot CLI capability without publishing it in results."""

    capability = context.state.pop(_CLI_LOCAL_FILE_IMPORT_KEY, None)
    return (
        type(capability) is tuple
        and len(capability) == 2
        and capability[0] is _CLI_LOCAL_FILE_IMPORT_TOKEN
        and capability[1] == source
    )


def _is_http_source(value: str) -> bool:
    """Return whether ``value`` belongs to the published remote URL route."""

    return value.strip().lower().startswith(("http://", "https://"))


def _literal_text_processor(processor_name: str, text: str):
    """Select a processor for literal text without any filesystem probing."""

    from src.processors.ai_chat_processor import AIChatProcessor
    from src.processors.text_fallback_processor import TextFallbackProcessor

    if processor_name == "auto":
        if AIChatProcessor._looks_like_ai_chat(text):
            return AIChatProcessor()
        return TextFallbackProcessor()
    if processor_name in {"ai_chat", "text_fallback"}:
        return get_processor_by_name(processor_name)
    raise PKVRuntimeError(
        ErrorCode.WORKFLOW_STEP_FAILED,
        "非 URL 输入需要显式的本地文件能力或纯文本处理器",
        stage="workflow_local_file_capability",
        recoverable=False,
    )


async def _auto_local_file_processor(source: str):
    """Classify an already-authorized file through one verified content read."""

    from src.processors.ai_chat_processor import AIChatProcessor
    from src.processors.chat_processor import ChatProcessor
    from src.processors.local_file_reader import read_local_text_file
    from src.processors.text_fallback_processor import TextFallbackProcessor

    if ChatProcessor.can_handle(source):
        return ChatProcessor()
    sample = await asyncio.to_thread(
        read_local_text_file,
        Path(source),
        errors="ignore",
    )
    if AIChatProcessor._looks_like_ai_chat(sample):
        return AIChatProcessor()
    return TextFallbackProcessor()


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
        if type(url) is not str or not url.strip():
            message = f"缺少 URL 输入: {url_key}"
            self._log(context, message)
            return {"errors": [message]}

        trusted_local_file = _consume_cli_local_file_import(context, url)

        processor_name = normalize_processor_name(
            str(self.config.get("processor", "auto"))
        )
        retry_times = int(self.config.get("retry", 0))
        timeout = float(self.config.get("timeout", 30))

        last_error: Optional[str] = None
        last_issue: Optional[Dict[str, Any]] = None
        for attempt in range(retry_times + 1):
            try:
                if trusted_local_file:
                    if processor_name != "auto":
                        self._log(context, f"使用指定处理器: {processor_name}")
                        processor = get_processor_by_name(processor_name)
                    else:
                        processor = await _auto_local_file_processor(url)
                    process_file = getattr(processor, "process_file", None)
                    if not callable(process_file):
                        raise PKVRuntimeError(
                            ErrorCode.WORKFLOW_STEP_FAILED,
                            "所选处理器不支持显式本地文件导入",
                            stage="workflow_local_file_capability",
                            recoverable=False,
                        )
                    entry = await asyncio.wait_for(
                        process_file(url),
                        timeout=timeout,
                    )
                elif not _is_http_source(url):
                    processor = _literal_text_processor(processor_name, url)
                    process_text = getattr(processor, "process_text", None)
                    if not callable(process_text):
                        raise PKVRuntimeError(
                            ErrorCode.WORKFLOW_STEP_FAILED,
                            "所选处理器不支持纯文本输入",
                            stage="workflow_local_file_capability",
                            recoverable=False,
                        )
                    entry = await asyncio.wait_for(
                        process_text(url),
                        timeout=timeout,
                    )
                elif processor_name != "auto":
                    self._log(context, f"使用指定处理器: {processor_name}")
                    processor = get_processor_by_name(processor_name)
                    entry = await asyncio.wait_for(
                        processor.process(url),
                        timeout=timeout,
                    )
                else:
                    processor = get_processor(url)
                    entry = await asyncio.wait_for(
                        processor.process(url),
                        timeout=timeout,
                    )
                title_length = len(entry.title) if isinstance(entry.title, str) else 0
                content_length = (
                    len(entry.content) if isinstance(entry.content, str) else 0
                )
                self._log(
                    context,
                    "抓取完成: "
                    f"title_length={title_length}, content_length={content_length}",
                )
                result: Dict[str, Any] = {
                    "entry": entry,
                    "content": entry.content,
                    "title": entry.title,
                    "source_type": entry.source_type,
                    "source_url": entry.source_url,
                }
                processing_issues = getattr(entry, "processing_issues", None)
                if isinstance(processing_issues, list):
                    stable_issues = [
                        dict(issue)
                        for issue in processing_issues
                        if isinstance(issue, dict)
                    ]
                    if stable_issues:
                        result["issues"] = stable_issues
                        result["warnings"] = [
                            str(issue["message"])
                            for issue in stable_issues
                            if isinstance(issue.get("message"), str)
                            and issue["message"]
                        ]
                return result
            except Exception as e:
                logger.error(
                    "Workflow fetch failed: step_id=%s attempt=%s cause_type=%s",
                    self.step_id,
                    attempt + 1,
                    type(e).__name__,
                )
                if isinstance(e, PKVRuntimeError) and e.code in {
                    ErrorCode.URL_INVALID,
                    ErrorCode.SSRF_TARGET_FORBIDDEN,
                    ErrorCode.SSRF_RESOLUTION_FAILED,
                    ErrorCode.SSRF_REDIRECT_LIMIT,
                }:
                    message = "URL 安全校验失败"
                    issue = e.to_dict()
                    issue.update(
                        {
                            "message": message,
                            "severity": "error",
                            "cause_type": type(e).__name__,
                        }
                    )
                    self._log(context, message)
                    return {"errors": [message], "issues": [issue]}
                last_error = "抓取失败"
                last_issue = {
                    "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                    "message": last_error,
                    "severity": "error",
                    "recoverable": attempt < retry_times,
                    "stage": "workflow_fetch",
                    "cause_type": type(e).__name__,
                }
                self._log(context, f"{last_error} (第 {attempt + 1}/{retry_times + 1} 次)")
                if attempt < retry_times:
                    await asyncio.sleep(1)

        result: Dict[str, Any] = {"errors": [last_error] if last_error else ["抓取失败"]}
        if last_issue is not None:
            last_issue["recoverable"] = False
            result["issues"] = [last_issue]
        return result


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
        issues: List[Dict[str, Any]] = []

        config = get_config()
        model = config.llm_model
        max_words = int(self.config.get("max_words", 300))
        num_tags = int(self.config.get("num_tags", 5))

        client = self._client or DeepSeekClient(model=model)

        if "summarize" in tasks:
            try:
                summary = await asyncio.to_thread(client.summarize, content, max_words)
            except Exception as e:
                logger.error(
                    "Workflow summary generation failed: step_id=%s cause_type=%s",
                    self.step_id,
                    type(e).__name__,
                )
                message = "摘要生成失败"
                errors.append(message)
                issues.append(
                    {
                        "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                        "message": message,
                        "severity": "error",
                        "recoverable": True,
                        "stage": "workflow_analyze",
                        "cause_type": type(e).__name__,
                    }
                )

        if "extract_tags" in tasks:
            try:
                tags = await asyncio.to_thread(client.extract_tags, content, num_tags)
            except Exception as e:
                logger.error(
                    "Workflow tag extraction failed: step_id=%s cause_type=%s",
                    self.step_id,
                    type(e).__name__,
                )
                message = "标签提取失败"
                errors.append(message)
                issues.append(
                    {
                        "code": ErrorCode.WORKFLOW_STEP_FAILED.value,
                        "message": message,
                        "severity": "error",
                        "recoverable": True,
                        "stage": "workflow_analyze",
                        "cause_type": type(e).__name__,
                    }
                )

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
            result["issues"] = issues

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
        if context.state.get("skip_sharpen"):
            self._log(context, "skip_sharpen=True，跳过 Idea Sharpen")
            return {}

        if not self._should_run(context):
            self._log(context, "条件不满足，跳过 Idea Sharpen")
            return {}

        questions = self.config.get("questions") or []
        if not questions:
            self._log(context, "未配置问题，跳过 Idea Sharpen")
            return {}

        timeout_value = self.config.get("timeout")
        skip_on_timeout = bool(self.config.get("skip_on_timeout", True))

        console = Console()
        answers: Dict[str, str] = {}

        for question in questions:
            console.print(Panel(question, title="Idea Sharpen"))
            try:
                prompt = asyncio.to_thread(Prompt.ask, "你的回答")
                if timeout_value is None:
                    answer = await prompt
                else:
                    answer = await asyncio.wait_for(prompt, timeout=float(timeout_value))
                answers[question] = answer
            except asyncio.TimeoutError:
                if timeout_value is None:
                    raise
                message = "用户响应超时，已跳过 Idea Sharpen"
                self._log(context, message)
                if skip_on_timeout:
                    return {"idea_sharpen": answers, "warnings": [message]}
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
            "content": content,
        }

        trigger_rules = self.config.get("trigger_rules")
        condition = self.config.get("condition")
        trigger_matches = True
        condition_matches = True
        if trigger_rules is not None:
            validate_trigger_rules(trigger_rules, step_id=self.step_id)
            trigger_matches = evaluate_trigger_rules(trigger_rules, variables)
        if condition is not None:
            condition_matches = evaluate_condition(condition, variables)
        return trigger_matches and condition_matches


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
        # 检查是否被审核步骤拒绝
        if context.state.get("review_rejected") or context.state.get("review_blocked"):
            message = (
                "审核未完成，存储步骤已跳过"
                if context.state.get("review_blocked")
                else "用户拒绝入库，存储步骤已跳过"
            )
            self._log(context, message)
            return {
                "review_rejected": bool(context.state.get("review_rejected")),
                "review_blocked": bool(context.state.get("review_blocked")),
                "errors": [message],
            }

        entry: Optional[Entry] = context.state.get("entry")
        if entry is None:
            message = "缺少 Entry，无法存储"
            self._log(context, message)
            return {"errors": [message]}

        targets = self.config.get("targets") or ["markdown"]
        config = get_config()

        required_targets = {"markdown", "sqlite"}
        if not required_targets.issubset(set(targets)):
            missing = sorted(required_targets - set(targets))
            errors = [
                "W1 存储合同要求 Markdown 主存储与 SQLite/FTS/chunk 索引同时启用；"
                f"缺少: {', '.join(missing)}"
            ]
            if "vector_index" in targets:
                errors.append("缺少 knowledge_id，跳过向量索引")
            self._log(context, "存储目标不满足 W1 核心合同")
            return {
                "file_path": None,
                "knowledge_id": None,
                "stored_targets": targets,
                "entry": entry,
                "status": "rejected",
                "stage": "preparing",
                "errors": errors,
            }

        markdown_store = self._markdown_store or MarkdownStore(config.vault_dir)
        sqlite_store = self._sqlite_store or SQLiteStore(config.db_path)
        coordinator = StorageCoordinator(
            markdown_store,
            sqlite_store,
            config.layout.runtime_state_dir / "operations",
        )

        chunks: Optional[List[str]] = None
        vector_operation = None
        vector_error: Optional[BaseException] = None
        vector_required = "vector_index" in targets
        if vector_required:
            try:
                embedder = self._embedder or Embedder()

                def prepare_vectors():
                    doc_vector = embedder.embed_document(entry.content)
                    chunk_vectors, prepared_chunks = embedder.embed_chunks(
                        entry.content, True
                    )
                    resolved_dim = getattr(embedder, "dim", None)
                    if resolved_dim is None and getattr(doc_vector, "shape", None):
                        resolved_dim = int(doc_vector.shape[-1])
                    if resolved_dim is None and hasattr(embedder, "resolve_dim"):
                        resolved_dim = embedder.resolve_dim()
                    vector_store = self._vector_store or VectorStore(
                        index_dir=config.vector_index_dir,
                        dim=resolved_dim,
                    )
                    return doc_vector, chunk_vectors, prepared_chunks or [], vector_store

                (
                    doc_vector,
                    chunk_vectors,
                    chunks,
                    vector_store,
                ) = await asyncio.to_thread(prepare_vectors)

                def write_vectors(knowledge_id: int) -> None:
                    vector_store.add_doc_vector(knowledge_id, doc_vector)
                    chunk_indices = list(range(len(chunk_vectors)))
                    if hasattr(vector_store, "add_chunk_vectors"):
                        vector_store.add_chunk_vectors(
                            knowledge_id,
                            chunk_indices,
                            chunk_vectors,
                        )
                    else:
                        for index, vector in enumerate(chunk_vectors):
                            vector_store.add_chunk_vector(knowledge_id, index, vector)

                vector_operation = write_vectors
            except Exception as exc:
                vector_error = exc

        operation = await asyncio.to_thread(
            coordinator.archive,
            entry,
            chunks=chunks,
            vector_operation=vector_operation,
            vector_error=vector_error,
            vector_required=vector_required,
        )
        result = operation.to_dict()
        result.update(
            {
                "stored_targets": targets,
                "entry": entry,
            }
        )
        stable_errors = list(operation.errors)
        result["storage_errors"] = stable_errors
        # WorkflowEngine consumes the human-readable ``errors`` key. Preserve
        # machine-readable W1 codes separately, and treat DEGRADED as a committed
        # core result with repair warnings rather than a retry-safe rejection.
        result.pop("errors", None)
        if stable_errors:
            messages = [
                f"{error['code']}: {error['message']}" for error in operation.errors
            ]
            if operation.status is OperationStatus.DEGRADED:
                result["warnings"] = messages
            else:
                result["errors"] = messages
                if not operation.retry_safe:
                    # Markdown+SQLite may already be committed: retrying blindly
                    # would duplicate facts.  Surface a committed-needs-repair
                    # warning instead of a retry-safe generic failure.
                    do_not_retry = (
                        f"do_not_retry: 核心存储已提交或需先修复（operation_id="
                        f"{operation.operation_id}），请勿盲目重试归档"
                    )
                    result["errors"] = [*messages, do_not_retry]
                    result["warnings"] = [do_not_retry]
            self._log(context, f"存储终态: {operation.status.value}")
        return result


class ReviewStep(BaseStep):
    """
    用户审核步骤（CLI 交互式人工审核）。

    在 AI 分析之后、持久化存储之前，让用户审查并修改 AI 生成的摘要和标签。
    支持：修改摘要、修改标签、添加评论、AI 重新生成、查看历史、通过或拒绝入草稿区。
    """

    def __init__(
        self,
        step_id: str,
        config: Dict[str, Any],
        review_manager: Optional[Any] = None,
        deepseek_client: Optional[DeepSeekClient] = None,
    ) -> None:
        """
        初始化审核步骤。

        Args:
            step_id: 步骤 ID
            config: 步骤配置
            review_manager: 可注入的 ReviewManager（用于测试）
            deepseek_client: 可注入的 DeepSeek 客户端（用于测试）
        """
        super().__init__(step_id, config)
        self._review_manager = review_manager
        self._deepseek_client = deepseek_client

    async def execute(self, context: WorkflowContext) -> Dict[str, Any]:
        """
        执行审核交互流程。

        Args:
            context: 工作流上下文

        Returns:
            审核结果字典，包含 review_id、review_status、review_rejected 等字段
        """
        from src.storage.review_manager import ReviewItem, ReviewManager

        entry: Optional[Entry] = context.state.get("entry")
        if entry is None:
            self._log(context, "缺少 Entry，跳过审核")
            return {"errors": ["缺少 Entry，跳过审核"]}

        # 跳过审核条件
        if context.state.get("skip_review"):
            self._log(context, "skip_review=True，跳过审核步骤")
            return {}
        if not self.config.get("required", True) and context.state.get("skip_sharpen"):
            self._log(context, "非必须审核且 skip_sharpen=True，跳过审核步骤")
            return {}

        review_required = bool(self.config.get("required", True))
        if review_required:
            # Fail closed: any exception before explicit approval leaves this
            # guard set, so an on_error=continue review cannot leak into storage.
            context.state.set("review_blocked", True)

        # 初始化数据
        summary: str = entry.summary_100_words or ""
        tags: List[str] = entry.tags if isinstance(entry.tags, list) else []
        content_preview: str = (entry.content or "")[: self.config.get("preview_chars", 500)]

        # 创建审核队列条目
        review_manager = self._review_manager or ReviewManager()
        item = ReviewItem(
            ai_generated_summary=summary,
            ai_generated_tags=",".join(tags),
            source_type=getattr(entry, "source_type", None) or "unknown",
            original_content_preview=content_preview,
            source_url=(
                sanitize_public_source_url(getattr(entry, "source_url", None))
                or None
            ),
        )
        review_id = await asyncio.to_thread(review_manager.create_review, item)
        self._log(context, f"审核条目已创建: review_id={review_id}")

        # Published schema-v1 review has no fake-cancellable stdin timeout.
        # Direct legacy construction may still provide one for compatibility.
        timeout_value = self.config.get("timeout")
        try:
            review = self._interactive_review(
                review_id, review_manager, entry, summary, tags, context
            )
            if timeout_value is None:
                final_decision = await review
            else:
                final_decision = await asyncio.wait_for(review, timeout=float(timeout_value))
        except asyncio.TimeoutError:
            if timeout_value is None:
                raise
            timeout = float(timeout_value) if timeout_value is not None else 0
            if self.config.get("skip_on_timeout", False):
                message = f"审核超时（{timeout}s），保留待审核并阻止入库"
                self._log(context, message)
                context.state.set("review_blocked", True)
                return {
                    "review_id": review_id,
                    "review_status": "pending",
                    "review_blocked": True,
                    "warnings": [message],
                }
            if "skip_on_timeout" in self.config:
                context.state.set("review_blocked", True)
                raise
            # Direct legacy construction did not have this YAML field. Preserve
            # its old behavior; published schema-v1 workflows must be explicit.
            self._log(context, f"审核超时（{timeout}s），按 legacy 默认自动通过")
            final_decision = "approve"

        # 处理最终决定
        if final_decision == "reject":
            await asyncio.to_thread(review_manager.reject_review, review_id)
            context.state.set("review_rejected", True)
            context.state.set("review_blocked", False)
            return {
                "review_id": review_id,
                "review_status": "rejected",
                "review_rejected": True,
            }
        else:
            await asyncio.to_thread(review_manager.approve_review, review_id)
            # 将用户最终版写回 entry
            final_item = await asyncio.to_thread(review_manager.get_review, review_id)
            if final_item is None:
                raise PKVRuntimeError(
                    ErrorCode.WORKFLOW_STEP_FAILED,
                    "审核条目在批准后不可用",
                    stage="workflow_review",
                    recoverable=True,
                )
            entry.summary_100_words = final_item.get_effective_summary()
            entry.tags = final_item.get_effective_tags()
            if final_item.user_comments:
                entry.notes = (
                    f"{entry.notes}\n{final_item.user_comments}".strip()
                    if entry.notes
                    else final_item.user_comments
                )
            context.state.set("entry", entry)
            context.state.set("review_blocked", False)
            return {
                "entry": entry,
                "review_id": review_id,
                "review_status": "approved",
                "review_rejected": False,
            }

    async def _interactive_review(
        self,
        review_id: int,
        review_manager: Any,
        entry: Entry,
        summary: str,
        tags: List[str],
        context: WorkflowContext,
    ) -> str:
        """
        交互式审核主循环。

        Args:
            review_id: 审核条目 ID
            review_manager: ReviewManager 实例
            entry: 当前 Entry
            summary: 当前摘要（可能被 AI 重新生成更新）
            tags: 当前标签列表（可能被 AI 重新生成更新）
            context: 工作流上下文

        Returns:
            最终决定字符串："approve" 或 "reject"
        """
        from rich.table import Table

        console = Console()
        max_regen = int(self.config.get("max_regenerations", 3))

        while True:
            # 显示当前审核内容
            current_item = await asyncio.to_thread(review_manager.get_review, review_id)
            if current_item is None:
                raise PKVRuntimeError(
                    ErrorCode.WORKFLOW_STEP_FAILED,
                    "审核条目不可用",
                    stage="workflow_review",
                    recoverable=True,
                )

            current_summary = current_item.get_effective_summary()
            current_tags = current_item.get_effective_tags()

            table = Table(title=f"审核条目 #{review_id}", show_header=True, header_style="bold cyan")
            table.add_column("字段", style="bold", width=16)
            table.add_column("内容", overflow="fold")

            table.add_row("来源类型", current_item.source_type)
            if current_item.source_url:
                table.add_row("来源 URL", current_item.source_url)
            table.add_row("AI 摘要", current_summary)
            table.add_row("当前标签", ", ".join(current_tags) or "（无）")
            if current_item.user_comments:
                table.add_row("个人评论", current_item.user_comments)
            table.add_row("重生成次数", str(current_item.regeneration_count))

            console.print(table)

            # 显示操作菜单
            menu = Panel(
                "[a] 通过审核 → 入库\n"
                "[m] 修改摘要\n"
                "[t] 修改标签\n"
                "[c] 添加个人评论\n"
                "[r] AI 重新生成\n"
                "[h] 查看修改历史\n"
                "[d] 拒绝 → 存入草稿区",
                title="[bold yellow]审核菜单[/bold yellow]",
                border_style="yellow",
            )
            console.print(menu)

            choice = await asyncio.to_thread(
                Prompt.ask,
                "请选择操作",
                choices=["a", "m", "t", "c", "r", "h", "d"],
                default="a",
            )

            if choice == "a":
                # 通过审核
                console.print("[bold green]审核通过，准备入库...[/bold green]")
                return "approve"

            elif choice == "d":
                # 拒绝，存入草稿区
                console.print("[bold red]已拒绝，内容将存入草稿区。[/bold red]")
                return "reject"

            elif choice == "m":
                # 修改摘要
                console.print(
                    Panel(
                        f"[dim]当前摘要:[/dim]\n{current_summary}",
                        title="编辑摘要",
                        border_style="blue",
                    )
                )
                edit_choice = await asyncio.to_thread(
                    Prompt.ask,
                    "选择编辑方式: [e]打开编辑器 / [i]直接输入",
                    choices=["e", "i"],
                    default="i",
                )
                if edit_choice == "e":
                    new_summary = await asyncio.to_thread(self._open_editor, current_summary)
                else:
                    new_summary = await asyncio.to_thread(Prompt.ask, "请输入新摘要")

                if new_summary and new_summary.strip():
                    await asyncio.to_thread(
                        review_manager.update_user_summary, review_id, new_summary.strip()
                    )
                    console.print("[green]摘要已更新。[/green]")
                else:
                    console.print("[yellow]摘要未修改。[/yellow]")

            elif choice == "t":
                # 修改标签
                console.print(
                    Panel(
                        f"[dim]当前标签:[/dim] {', '.join(current_tags) or '（无）'}",
                        title="编辑标签",
                        border_style="blue",
                    )
                )
                tags_input = await asyncio.to_thread(
                    Prompt.ask,
                    "请输入新标签（逗号分隔，直接回车保持不变）",
                    default="",
                )
                if tags_input.strip():
                    new_tags = [t.strip() for t in tags_input.split(",") if t.strip()]
                    await asyncio.to_thread(
                        review_manager.update_user_tags, review_id, new_tags
                    )
                    console.print(f"[green]标签已更新: {', '.join(new_tags)}[/green]")
                else:
                    console.print("[yellow]标签未修改。[/yellow]")

            elif choice == "c":
                # 添加个人评论
                comment = await asyncio.to_thread(Prompt.ask, "请输入个人评论")
                if comment.strip():
                    await asyncio.to_thread(
                        review_manager.add_user_comment, review_id, comment.strip()
                    )
                    console.print("[green]评论已添加。[/green]")
                else:
                    console.print("[yellow]未添加评论。[/yellow]")

            elif choice == "r":
                # AI 重新生成
                if current_item.regeneration_count >= max_regen:
                    console.print(
                        f"[yellow]已达到最大重新生成次数（{max_regen}次），无法继续重生成。[/yellow]"
                    )
                    continue

                regen_prompt = await asyncio.to_thread(
                    Prompt.ask,
                    "请输入对 AI 的指导（例如：更简洁/聚焦技术细节）",
                )
                if not regen_prompt.strip():
                    console.print("[yellow]未输入指导，取消重生成。[/yellow]")
                    continue

                console.print("[dim]正在调用 AI 重新生成，请稍候...[/dim]")
                try:
                    new_summary, new_tags = await self._call_ai_regenerate(
                        content=current_item.original_content_preview,
                        summary=current_summary,
                        tags=current_tags,
                        prompt=regen_prompt.strip(),
                    )
                    await asyncio.to_thread(
                        review_manager.record_regeneration,
                        review_id,
                        regen_prompt.strip(),
                        new_summary,
                        new_tags,
                    )
                    console.print(
                        Panel(
                            f"[bold]新摘要:[/bold] {new_summary}\n"
                            f"[bold]新标签:[/bold] {', '.join(new_tags)}",
                            title="[green]AI 重新生成结果[/green]",
                            border_style="green",
                        )
                    )
                except Exception as e:
                    logger.error(
                        "Workflow review regeneration failed: step_id=%s cause_type=%s",
                        self.step_id,
                        type(e).__name__,
                    )
                    console.print("[red]AI 重新生成失败，请稍后重试。[/red]")

            elif choice == "h":
                # 查看修改历史
                history = await asyncio.to_thread(review_manager.get_history, review_id)
                if not history:
                    console.print("[yellow]暂无操作历史。[/yellow]")
                else:
                    hist_table = Table(title="修改历史", show_header=True, header_style="bold")
                    hist_table.add_column("#", width=4)
                    hist_table.add_column("操作", width=16)
                    hist_table.add_column("操作人", width=8)
                    hist_table.add_column("时间", width=20)
                    hist_table.add_column("详情", overflow="fold")
                    for record in history:
                        hist_table.add_row(
                            str(record["history_id"]),
                            record["action"],
                            record["operator"],
                            str(record["created_at"]),
                            str(record["details"]),
                        )
                    console.print(hist_table)

    async def _call_ai_regenerate(
        self,
        content: str,
        summary: str,
        tags: List[str],
        prompt: str,
    ) -> tuple:
        """
        调用 DeepSeek API 重新生成摘要和标签。

        Args:
            content: 原始内容预览
            summary: 当前摘要
            tags: 当前标签列表
            prompt: 用户指导

        Returns:
            (new_summary, new_tags) 元组
        """
        config = get_config()
        model = config.llm_model
        client = self._deepseek_client or DeepSeekClient(model=model)

        messages = [
            {
                "role": "system",
                "content": "你是知识管理助手，帮助改进内容摘要和标签。",
            },
            {
                "role": "user",
                "content": (
                    f"原始内容：{content[:1000]}\n"
                    f"当前摘要：{summary}\n"
                    f"当前标签：{', '.join(tags)}\n"
                    f"用户指导：{prompt}\n\n"
                    "请根据用户指导重新生成摘要和标签。返回格式：\n"
                    "摘要：...\n"
                    "标签：tag1, tag2, tag3"
                ),
            },
        ]

        response = await asyncio.to_thread(client._call_api, messages)

        # 解析响应
        new_summary = summary
        new_tags = tags

        for line in response.splitlines():
            line = line.strip()
            if line.startswith("摘要：") or line.startswith("摘要:"):
                new_summary = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            elif line.startswith("标签：") or line.startswith("标签:"):
                tags_raw = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                new_tags = [t.strip() for t in tags_raw.split(",") if t.strip()]

        return new_summary, new_tags

    @staticmethod
    def _open_editor(content: str) -> Optional[str]:
        """
        调用系统编辑器让用户编辑文本。

        Args:
            content: 初始内容（写入临时文件）

        Returns:
            编辑后内容字符串，或 None（编辑器返回非零退出码时）
        """
        import os
        import subprocess
        import tempfile

        from src.runtime.layout import verify_fd_matches_path

        editor = os.environ.get("EDITOR", "vim")
        config = get_config()
        layout = config.layout
        layout.ensure_user_directories()
        tmp_dir = layout.validate_user_directory(
            layout.tmp_dir,
            label="审核编辑临时目录",
            allow_missing=False,
        )
        parent_before = os.lstat(tmp_dir)
        descriptor, raw_path = tempfile.mkstemp(
            suffix=".txt",
            prefix="review-editor-",
            dir=tmp_dir,
        )
        tmp_path = Path(raw_path)
        descriptor_open = True

        try:
            parent_after = os.lstat(tmp_dir)
            if (parent_after.st_dev, parent_after.st_ino) != (
                parent_before.st_dev,
                parent_before.st_ino,
            ):
                raise PKVRuntimeError(
                    ErrorCode.DATA_ROOT_UNSAFE,
                    "审核编辑临时目录在创建文件期间被替换",
                    stage="workflow_review_editor",
                    recoverable=False,
                )
            verify_fd_matches_path(
                descriptor,
                tmp_path,
                label="审核编辑临时文件",
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as temp_file:
                descriptor_open = False
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())

            result = subprocess.call([editor, tmp_path])
            if result == 0:
                with layout.open_user_file(
                    tmp_path,
                    "r",
                    label="审核编辑临时文件",
                    encoding="utf-8",
                ) as temp_file:
                    return temp_file.read()
        except Exception:
            return None
        finally:
            if descriptor_open:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                layout.validate_user_directory(
                    tmp_dir,
                    label="审核编辑临时目录",
                    allow_missing=False,
                )
                safe_path = layout.validate_user_file(
                    tmp_path,
                    label="审核编辑临时文件",
                    allow_missing=True,
                )
                if os.path.lexists(safe_path):
                    safe_path.unlink()
            except Exception:
                pass
        return None
