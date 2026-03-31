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
        # 检查是否被审核步骤拒绝
        if context.state.get("review_rejected"):
            message = "用户拒绝入库，存储步骤已跳过"
            self._log(context, message)
            return {"review_rejected": True, "errors": [message]}

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
                    embedder = self._embedder or Embedder()
                    resolved_dim = getattr(embedder, "dim", None)
                    if resolved_dim is None and hasattr(embedder, "resolve_dim"):
                        resolved_dim = embedder.resolve_dim()
                    vector_store = self._vector_store or VectorStore(
                        index_dir=config.vector_index_dir,
                        dim=resolved_dim,
                    )
                    sqlite_store = self._sqlite_store or SQLiteStore(config.db_path)
                    doc_vector = await asyncio.to_thread(embedder.embed_document, entry.content)
                    vector_store.add_doc_vector(knowledge_id, doc_vector)

                    chunk_vectors, chunks = await asyncio.to_thread(
                        embedder.embed_chunks, entry.content, True
                    )
                    sqlite_store.insert_chunks(knowledge_id, chunks or [])
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
            source_url=getattr(entry, "source_url", None),
        )
        review_id = await asyncio.to_thread(review_manager.create_review, item)
        self._log(context, f"审核条目已创建: review_id={review_id}")

        # 带超时的交互式审核
        timeout = int(self.config.get("timeout", 600))
        try:
            final_decision = await asyncio.wait_for(
                self._interactive_review(
                    review_id, review_manager, entry, summary, tags, context
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self._log(context, f"审核超时（{timeout}s），自动通过")
            final_decision = "approve"

        # 处理最终决定
        if final_decision == "reject":
            await asyncio.to_thread(review_manager.reject_review, review_id)
            context.state.set("review_rejected", True)
            return {
                "review_id": review_id,
                "review_status": "rejected",
                "review_rejected": True,
            }
        else:
            await asyncio.to_thread(review_manager.approve_review, review_id)
            # 将用户最终版写回 entry
            final_item = await asyncio.to_thread(review_manager.get_review, review_id)
            if final_item is not None:
                entry.summary_100_words = final_item.get_effective_summary()
                entry.tags = final_item.get_effective_tags()
                if final_item.user_comments:
                    entry.notes = (
                        f"{entry.notes}\n{final_item.user_comments}".strip()
                        if entry.notes
                        else final_item.user_comments
                    )
            context.state.set("entry", entry)
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
                self._log(context, "无法获取审核条目，自动通过")
                return "approve"

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
                    console.print(f"[red]AI 重新生成失败: {e}[/red]")

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
        model = config.get("ai.deepseek.model", "deepseek-chat")
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

        editor = os.environ.get("EDITOR", "vim")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            tmp_path = f.name

        try:
            result = subprocess.call([editor, tmp_path])
            if result == 0:
                with open(tmp_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return None
