"""
Workflow steps unit tests.
"""

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.processors.text_fallback_processor import TextFallbackProcessor
from src.storage.coordinator import StorageCoordinator
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.migration_manager import MigrationManager
from src.storage.sqlite_store import SQLiteStore
from src.workflow.models import WorkflowContext
from src.utils.config import Config
import src.workflow.steps as workflow_steps
from src.workflow.steps import (
    AnalyzeStep,
    FetchStep,
    IdeaSharpenStep,
    ReviewStep,
    StoreStep,
)


class DummyProcessor:
    """Dummy processor for fetch step tests."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Always handle the URL."""
        return True

    async def process(self, url: str) -> Entry:
        """Return a simple Entry."""
        return Entry(
            title="Dummy Title",
            source_type="generic",
            source_url=url,
            content="Hello world",
        )


class DummyDeepSeekClient:
    """Dummy DeepSeek client for analyze step tests."""

    def summarize(self, content: str, max_words: int = 300) -> str:
        """Return a fixed summary."""
        return "summary text"

    def extract_tags(self, content: str, num_tags: int = 5) -> list[str]:
        """Return a fixed tag list."""
        return ["tag1", "tag2", "tag3"]


class FailingDeepSeekClient:
    """DeepSeek client that raises errors."""

    def summarize(self, content: str, max_words: int = 300) -> str:
        """Raise an error for summary."""
        raise RuntimeError("summarize error")

    def extract_tags(self, content: str, num_tags: int = 5) -> list[str]:
        """Raise an error for tags."""
        raise RuntimeError("tag error")


class FailingProcessor:
    """Processor that always fails."""

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Always handle the URL."""
        return True

    async def process(self, url: str) -> Entry:
        """Always raise a fetch error."""
        raise RuntimeError("fetch error")


class DummyVectorStore:
    """Dummy vector store to capture add calls."""

    def __init__(self) -> None:
        self.doc_calls = []
        self.chunk_calls = []

    def add_doc_vector(self, knowledge_id: int, vector: np.ndarray) -> None:
        """Record doc vector calls."""
        self.doc_calls.append((knowledge_id, vector))

    def add_chunk_vector(self, knowledge_id: int, chunk_index: int, vector: np.ndarray) -> None:
        """Record chunk vector calls."""
        self.chunk_calls.append((knowledge_id, chunk_index, vector))


class DummyEmbedder:
    """Dummy embedder to avoid external API calls."""

    def embed_document(self, text: str) -> np.ndarray:
        """Return a zero vector for document."""
        return np.zeros(1536, dtype="float32")

    def embed_chunks(self, text: str, return_chunks: bool = False) -> tuple[np.ndarray, list[str] | None]:
        """Return dummy chunk vectors."""
        vectors = np.zeros((2, 1536), dtype="float32")
        chunks = ["chunk1", "chunk2"]
        return vectors, chunks if return_chunks else None


def get_processor_stub(_url: str) -> DummyProcessor:
    """Return a dummy processor instance."""
    return DummyProcessor()


def get_failing_processor_stub(_url: str) -> FailingProcessor:
    """Return a failing processor instance."""
    return FailingProcessor()


def prompt_stub(_prompt: str) -> str:
    """Return a static prompt answer."""
    return "answer"


@pytest.fixture
def isolated_steps_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Config:
    """Inject base-only Config at the StoreStep/AnalyzeStep import site."""
    data_root = tmp_path / "runtime"
    runtime_paths = {
        "DATA_DIR": data_root,
        "DB_PATH": data_root / "db" / "knowledge_vault.db",
        "VAULT_DIR": data_root / "vault",
        "VECTOR_DIR": data_root / "vectors",
        "LOG_DIR": data_root / "logs",
        "TMP_DIR": data_root / "tmp",
    }
    for key, path in runtime_paths.items():
        monkeypatch.setenv(key, str(path))

    config = Config(str(project_root / "config" / "config.yaml"))
    monkeypatch.setattr(workflow_steps, "get_config", lambda: config)
    return config


@pytest.mark.asyncio
async def test_fetch_step_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """FetchStep should return entry and content."""
    monkeypatch.setattr("src.workflow.steps.get_processor", get_processor_stub)

    context = WorkflowContext({"url": "https://example.com"})
    step = FetchStep(step_id="fetch", config={})
    result = await step.execute(context)

    assert "entry" in result
    assert result["content"] == "Hello world"
    assert result["title"] == "Dummy Title"


@pytest.mark.asyncio
async def test_fetch_step_missing_url() -> None:
    """FetchStep should report error when URL missing."""
    context = WorkflowContext({})
    step = FetchStep(step_id="fetch", config={})
    result = await step.execute(context)

    assert "errors" in result
    assert "缺少 URL 输入" in result["errors"][0]


@pytest.mark.asyncio
async def test_fetch_step_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """FetchStep should retry and return errors on failure."""
    async def fast_sleep(_seconds: float) -> None:
        """Fast sleep stub for retry tests."""
        return None

    selected_names: list[str] = []

    def get_named_failing_processor(name: str) -> FailingProcessor:
        selected_names.append(name)
        return FailingProcessor()

    monkeypatch.setattr(
        "src.workflow.steps.get_processor_by_name", get_named_failing_processor
    )
    monkeypatch.setattr("src.workflow.steps.asyncio.sleep", fast_sleep)

    context = WorkflowContext({"url": "https://example.com"})
    step = FetchStep(step_id="fetch", config={"retry": 1, "processor": "wechat"})
    result = await step.execute(context)

    assert "errors" in result
    assert "抓取失败" in result["errors"][0]
    assert selected_names == ["wechat", "wechat"]


@pytest.mark.asyncio
async def test_fetch_step_explicit_processor_never_uses_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_names: list[str] = []

    def select_named(name: str) -> DummyProcessor:
        selected_names.append(name)
        return DummyProcessor()

    def reject_auto(_url: str):
        raise AssertionError("explicit processor must not use auto routing")

    monkeypatch.setattr("src.workflow.steps.get_processor_by_name", select_named)
    monkeypatch.setattr("src.workflow.steps.get_processor", reject_auto)

    result = await FetchStep(
        step_id="fetch",
        config={"processor": "generic", "timeout": 1},
    ).execute(WorkflowContext({"url": "https://example.com"}))

    assert result["title"] == "Dummy Title"
    assert selected_names == ["generic"]


def _text_processor() -> TextFallbackProcessor:
    ai = MagicMock()
    ai.summarize.return_value = "summary"
    ai.extract_tags.return_value = ["note"]
    return TextFallbackProcessor(deepseek_client=ai)


@pytest.mark.asyncio
async def test_fetch_step_cli_capability_reads_existing_note_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "note.md"
    note.write_text("# Trusted note\nImported body", encoding="utf-8")
    processor = _text_processor()

    async def select_local(_source: str):
        return processor

    monkeypatch.setattr(workflow_steps, "_auto_local_file_processor", select_local)
    initial = {"url": str(note)}
    workflow_steps._grant_cli_local_file_import(initial, str(note))
    context = WorkflowContext(initial)

    result = await FetchStep(step_id="fetch", config={"timeout": 2}).execute(context)

    assert result["content"] == "# Trusted note\nImported body"
    assert result["title"] == "Trusted note"
    assert result["source_url"] is None
    assert workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY not in context.state.to_dict()


def test_transient_cli_capability_is_never_projected_to_workflow_result_data() -> None:
    initial = {"url": "note.md"}
    workflow_steps._grant_cli_local_file_import(initial, "note.md")
    context = WorkflowContext(initial)

    assert workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY in context.state.to_dict()
    assert workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY not in context.state.to_result_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [True, "true", "trusted", ("token", "path")])
async def test_fetch_step_forged_capability_never_reads_local_file(
    forged: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "WORKFLOW-LOCAL-FILE-SECRET-CANARY"
    note = tmp_path / "secret.md"
    note.write_text(secret, encoding="utf-8")
    processor = _text_processor()
    monkeypatch.setattr(
        workflow_steps,
        "_literal_text_processor",
        lambda _name, _text: processor,
    )

    def reject_registry(_url: str):
        raise AssertionError("untrusted path must bypass auto registry")

    monkeypatch.setattr(workflow_steps, "get_processor", reject_registry)
    context = WorkflowContext(
        {
            "url": str(note),
            workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY: forged,
        }
    )
    with monkeypatch.context() as path_guard:
        path_guard.setattr(
            Path,
            "exists",
            lambda _self: (_ for _ in ()).throw(
                AssertionError("untrusted path existence probe")
            ),
        )
        path_guard.setattr(
            Path,
            "read_text",
            lambda _self, *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("untrusted local file read")
            ),
        )
        result = await FetchStep(step_id="fetch", config={"timeout": 2}).execute(
            context
        )

    assert result["content"] == str(note)
    assert secret not in result["content"]
    assert workflow_steps._CLI_LOCAL_FILE_IMPORT_KEY not in context.state.to_dict()


@pytest.mark.asyncio
async def test_fetch_step_capability_is_bound_to_original_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("FIRST SECRET", encoding="utf-8")
    second.write_text("SECOND SECRET", encoding="utf-8")
    processor = _text_processor()
    monkeypatch.setattr(
        workflow_steps,
        "_literal_text_processor",
        lambda _name, _text: processor,
    )
    initial = {"url": str(first)}
    workflow_steps._grant_cli_local_file_import(initial, str(first))
    initial["url"] = str(second)

    with monkeypatch.context() as path_guard:
        path_guard.setattr(
            Path,
            "read_text",
            lambda _self, *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("path-bound capability reused for another source")
            ),
        )
        result = await FetchStep(step_id="fetch", config={"timeout": 2}).execute(
            WorkflowContext(initial)
        )

    assert result["content"] == str(second)
    assert "SECOND SECRET" not in result["content"]


@pytest.mark.asyncio
async def test_fetch_step_trusted_file_requires_explicit_file_processor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = tmp_path / "note.bin"
    note.write_bytes(b"content")

    async def select_local(_source: str):
        return DummyProcessor()

    monkeypatch.setattr(workflow_steps, "_auto_local_file_processor", select_local)
    initial = {"url": str(note)}
    workflow_steps._grant_cli_local_file_import(initial, str(note))

    result = await FetchStep(step_id="fetch", config={"retry": 0}).execute(
        WorkflowContext(initial)
    )

    assert result["errors"] == ["抓取失败"]
    assert result["issues"][0]["code"] == ErrorCode.WORKFLOW_STEP_FAILED.value


@pytest.mark.asyncio
async def test_auto_local_file_classifier_uses_verified_content_not_path_probe(
    tmp_path: Path,
) -> None:
    from src.processors.ai_chat_processor import AIChatProcessor
    from src.processors.chat_processor import ChatProcessor

    ai_chat = tmp_path / "conversation.md"
    ai_chat.write_text("**You**: Hello\n**ChatGPT**: Hi", encoding="utf-8")
    plain_note = tmp_path / "note.md"
    plain_note.write_text("# Plain note\nBody", encoding="utf-8")
    chat_log = tmp_path / "chat.txt"
    chat_log.write_text("Alice 10:00\n\nHello", encoding="utf-8")

    assert isinstance(
        await workflow_steps._auto_local_file_processor(str(ai_chat)),
        AIChatProcessor,
    )
    assert isinstance(
        await workflow_steps._auto_local_file_processor(str(plain_note)),
        TextFallbackProcessor,
    )
    assert isinstance(
        await workflow_steps._auto_local_file_processor(str(chat_log)),
        ChatProcessor,
    )


@pytest.mark.asyncio
async def test_fetch_step_logs_only_content_metadata(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Remote titles may flow in the result but never into default workflow logs."""

    title_canary = "REMOTE-TITLE-SECRET-CANARY\r\napi_key=secret"

    class CanaryProcessor:
        async def process(self, url: str) -> Entry:
            return Entry(
                title=title_canary,
                source_type="generic",
                source_url=url,
                content="safe body",
            )

    monkeypatch.setattr(
        "src.workflow.steps.get_processor",
        lambda _url: CanaryProcessor(),
    )
    caplog.set_level(logging.INFO, logger="src.workflow.models")
    context = WorkflowContext({"url": "https://example.com"})

    result = await FetchStep(step_id="fetch", config={}).execute(context)

    assert result["title"] == title_canary
    assert "title_length=" in "\n".join(context.logs)
    assert title_canary not in "\n".join(context.logs)
    assert "REMOTE-TITLE-SECRET-CANARY" not in caplog.text
    assert "api_key=secret" not in caplog.text


@pytest.mark.asyncio
async def test_fetch_step_projects_processor_resource_limit_as_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = {
        "code": ErrorCode.PROCESSOR_RESOURCE_LIMIT.value,
        "message": "页面图片达到资源预算，部分图片未下载",
        "severity": "warning",
        "recoverable": False,
        "stage": "wechat_image_count",
        "count": 2,
        "limit": 2,
    }

    class ResourceLimitedProcessor(DummyProcessor):
        async def process(self, url: str) -> Entry:
            entry = await super().process(url)
            entry.processing_issues = [issue]
            return entry

    monkeypatch.setattr(
        "src.workflow.steps.get_processor",
        lambda _url: ResourceLimitedProcessor(),
    )

    result = await FetchStep(step_id="fetch", config={}).execute(
        WorkflowContext({"url": "https://example.com"})
    )

    assert result["warnings"] == [issue["message"]]
    assert result["issues"] == [issue]


@pytest.mark.asyncio
async def test_fetch_step_does_not_retry_ssrf_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class SecurityRejectingProcessor:
        async def process(self, _url: str) -> Entry:
            nonlocal calls
            calls += 1
            raise PKVRuntimeError(
                ErrorCode.SSRF_RESOLUTION_FAILED,
                "secret target detail",
                stage="url_resolution",
                recoverable=True,
            )

    monkeypatch.setattr(
        "src.workflow.steps.get_processor",
        lambda _url: SecurityRejectingProcessor(),
    )
    step = FetchStep(step_id="fetch", config={"retry": 3, "timeout": 1})

    result = await step.execute(WorkflowContext({"url": "https://example.com"}))

    assert calls == 1
    assert result["errors"] == ["URL 安全校验失败"]
    assert result["issues"][0]["code"] == ErrorCode.SSRF_RESOLUTION_FAILED.value
    assert result["issues"][0]["recoverable"] is True
    assert "secret target detail" not in str(result)


@pytest.mark.asyncio
async def test_analyze_step_updates_entry(
    isolated_steps_config: Config,
) -> None:
    """AnalyzeStep should update entry summary and tags."""
    entry = Entry(
        title="Title",
        source_type="generic",
        content="Some content",
    )
    context = WorkflowContext({"entry": entry})
    step = AnalyzeStep(
        step_id="analyze",
        config={"tasks": ["summarize", "extract_tags"]},
        deepseek_client=DummyDeepSeekClient(),
    )

    result = await step.execute(context)

    assert result["summary"] == "summary text"
    assert result["tags"] == ["tag1", "tag2", "tag3"]
    assert result["content_length"] == len("Some content")
    assert entry.summary_100_words == "summary text"
    assert entry.tags == ["tag1", "tag2", "tag3"]


@pytest.mark.asyncio
async def test_analyze_step_errors(
    monkeypatch: pytest.MonkeyPatch,
    isolated_steps_config: Config,
) -> None:
    """AnalyzeStep should report errors when AI calls fail."""
    entry = Entry(
        title="Title",
        source_type="generic",
        content="Some content",
    )
    context = WorkflowContext({"entry": entry})
    step = AnalyzeStep(
        step_id="analyze",
        config={"tasks": ["summarize", "extract_tags", "extract_concepts"]},
        deepseek_client=FailingDeepSeekClient(),
    )

    result = await step.execute(context)

    assert "errors" in result
    assert any("摘要生成失败" in err for err in result["errors"])
    assert any("标签提取失败" in err for err in result["errors"])


def test_analyze_step_extract_first_sentence() -> None:
    """Extract first sentence should stop at delimiter."""
    sentence = AnalyzeStep._extract_first_sentence("第一句。第二句")
    assert sentence == "第一句"


@pytest.mark.asyncio
async def test_idea_sharpen_step_collects_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """IdeaSharpenStep should collect answers via prompt."""
    monkeypatch.setattr("src.workflow.steps.Prompt.ask", prompt_stub)

    async def reject_fake_timeout(*_args, **_kwargs):
        raise AssertionError("published config without timeout must not call wait_for")

    monkeypatch.setattr("src.workflow.steps.asyncio.wait_for", reject_fake_timeout)

    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1", "Q2"],
            "condition": "content_length > 0",
        },
    )

    result = await step.execute(context)

    assert "idea_sharpen" in result
    assert result["idea_sharpen"]["Q1"] == "answer"
    assert result["idea_sharpen"]["Q2"] == "answer"


@pytest.mark.asyncio
async def test_idea_sharpen_step_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """IdeaSharpenStep should skip on timeout when configured."""
    async def never_complete(*_args, **_kwargs) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr("src.workflow.steps.asyncio.to_thread", never_complete)

    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1"],
            "condition": "content_length > 0",
            "timeout": 0.01,
            "skip_on_timeout": True,
        },
    )

    result = await step.execute(context)
    assert result["idea_sharpen"] == {}
    assert result["warnings"]


@pytest.mark.asyncio
async def test_idea_sharpen_step_no_questions() -> None:
    """IdeaSharpenStep should skip when no questions provided."""
    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(step_id="sharpen", config={"questions": []})

    result = await step.execute(context)
    assert result == {}


@pytest.mark.asyncio
async def test_idea_sharpen_step_condition_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """IdeaSharpenStep must reject invalid conditions instead of silent success."""
    monkeypatch.setattr("src.workflow.steps.Prompt.ask", prompt_stub)

    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={"questions": ["Q1"], "condition": "invalid =="},
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        await step.execute(context)
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONDITION_INVALID


@pytest.mark.asyncio
async def test_idea_sharpen_condition_rejects_executable_syntax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.workflow.steps.Prompt.ask", prompt_stub)
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1"],
            "condition": "__import__('os').system('whoami')",
        },
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        await step.execute(WorkflowContext({"content_length": 10}))
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONDITION_INVALID


@pytest.mark.asyncio
async def test_idea_sharpen_skip_flag_has_no_prompt_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_prompt(*_args, **_kwargs):
        raise AssertionError("skip_sharpen must bypass prompts")

    monkeypatch.setattr("src.workflow.steps.Prompt.ask", reject_prompt)
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={"questions": ["Q1"], "condition": "content_length > 0"},
    )

    result = await step.execute(
        WorkflowContext({"content_length": 10, "skip_sharpen": True})
    )

    assert result == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        {"content_length": 3001, "content": "plain", "content_type": "generic"},
        {"content_length": 10, "content": "plain", "content_type": "wechat"},
        {"content_length": 10, "content": "包含观点对比", "content_type": "generic"},
    ],
)
async def test_idea_sharpen_trigger_rules_use_or_semantics(
    monkeypatch: pytest.MonkeyPatch,
    state: dict,
) -> None:
    monkeypatch.setattr("src.workflow.steps.Prompt.ask", prompt_stub)
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1"],
            "trigger_rules": [
                {"content_length_gt": 3000},
                {"content_type_in": ["wechat", "zhihu"]},
                {"contains_keywords": ["观点对比", "立场分析"]},
            ],
        },
    )

    result = await step.execute(WorkflowContext(state))

    assert result["idea_sharpen"] == {"Q1": "answer"}


@pytest.mark.asyncio
async def test_idea_sharpen_trigger_boundaries_all_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_prompt(*_args, **_kwargs):
        raise AssertionError("false trigger set must not prompt")

    monkeypatch.setattr("src.workflow.steps.Prompt.ask", reject_prompt)
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1"],
            "trigger_rules": [
                {"content_length_gt": 3000},
                {"content_type_in": ["wechat", "zhihu"]},
                {"contains_keywords": ["观点对比", "立场分析"]},
            ],
        },
    )

    result = await step.execute(
        WorkflowContext(
            {"content_length": 3000, "content": "plain", "content_type": "generic"}
        )
    )

    assert result == {}


@pytest.mark.asyncio
async def test_idea_sharpen_unknown_trigger_fails_closed() -> None:
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={"questions": ["Q1"], "trigger_rules": [{"run_python": "pass"}]},
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        await step.execute(WorkflowContext({"content": "plain"}))
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONFIG_INVALID


@pytest.mark.asyncio
async def test_idea_sharpen_invalid_trigger_state_has_stable_error() -> None:
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1"],
            "trigger_rules": [{"content_length_gt": 10}],
        },
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        await step.execute(WorkflowContext({"content_length": "many"}))
    assert exc_info.value.code is ErrorCode.WORKFLOW_CONDITION_INVALID


@pytest.mark.asyncio
async def test_published_idea_timeout_exception_is_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_timeout(_prompt: str) -> str:
        raise asyncio.TimeoutError

    monkeypatch.setattr("src.workflow.steps.Prompt.ask", raise_timeout)
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={"questions": ["Q1"], "condition": "content_length > 0"},
    )

    with pytest.raises(asyncio.TimeoutError):
        await step.execute(WorkflowContext({"content_length": 10}))


@pytest.mark.asyncio
async def test_idea_sharpen_step_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """IdeaSharpenStep should raise when skip_on_timeout is False."""
    async def never_complete(*_args, **_kwargs) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr("src.workflow.steps.asyncio.to_thread", never_complete)

    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1"],
            "condition": "content_length > 0",
            "timeout": 0.01,
            "skip_on_timeout": False,
        },
    )

    with pytest.raises(asyncio.TimeoutError):
        await step.execute(context)


@pytest.mark.asyncio
async def test_review_timeout_with_published_skip_policy_blocks_storage() -> None:
    manager = MagicMock()
    manager.create_review.return_value = 7
    step = ReviewStep(
        step_id="review",
        config={
            "required": True,
            "timeout": 1,
            "skip_on_timeout": True,
            "max_regenerations": 3,
            "preview_chars": 100,
        },
        review_manager=manager,
    )

    async def timeout_review(*_args, **_kwargs):
        raise asyncio.TimeoutError

    step._interactive_review = timeout_review  # type: ignore[method-assign]
    entry = Entry(title="pending", source_type="generic", content="content")
    context = WorkflowContext({"entry": entry})

    result = await step.execute(context)

    assert result["review_status"] == "pending"
    assert result["review_blocked"] is True
    assert result["warnings"]
    assert context.state.get("review_blocked") is True
    manager.approve_review.assert_not_called()


@pytest.mark.asyncio
async def test_required_review_missing_after_approval_stays_blocked() -> None:
    manager = MagicMock()
    manager.create_review.return_value = 8
    manager.get_review.return_value = None
    step = ReviewStep(
        step_id="review",
        config={
            "required": True,
            "timeout": 1,
            "skip_on_timeout": True,
            "max_regenerations": 3,
            "preview_chars": 100,
        },
        review_manager=manager,
    )

    async def approve(*_args, **_kwargs):
        return "approve"

    step._interactive_review = approve  # type: ignore[method-assign]
    context = WorkflowContext(
        {"entry": Entry(title="pending", source_type="generic", content="content")}
    )

    with pytest.raises(PKVRuntimeError) as exc_info:
        await step.execute(context)

    assert exc_info.value.code is ErrorCode.WORKFLOW_STEP_FAILED
    assert context.state.get("review_blocked") is True


@pytest.mark.asyncio
async def test_required_review_create_failure_stays_blocked() -> None:
    manager = MagicMock()
    manager.create_review.side_effect = RuntimeError("private db path")
    step = ReviewStep(
        step_id="review",
        config={
            "required": True,
            "timeout": 1,
            "skip_on_timeout": True,
            "max_regenerations": 3,
            "preview_chars": 100,
        },
        review_manager=manager,
    )
    context = WorkflowContext(
        {"entry": Entry(title="pending", source_type="generic", content="content")}
    )

    with pytest.raises(RuntimeError):
        await step.execute(context)

    assert context.state.get("review_blocked") is True


@pytest.mark.asyncio
async def test_published_review_without_timeout_avoids_fake_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    manager.create_review.return_value = 9
    final_item = MagicMock()
    final_item.get_effective_summary.return_value = "approved"
    final_item.get_effective_tags.return_value = ["tag"]
    final_item.user_comments = ""
    manager.get_review.return_value = final_item
    step = ReviewStep(
        step_id="review",
        config={"required": True, "max_regenerations": 3, "preview_chars": 100},
        review_manager=manager,
    )

    async def approve(*_args, **_kwargs):
        return "approve"

    async def reject_fake_timeout(*_args, **_kwargs):
        raise AssertionError("published config without timeout must not call wait_for")

    step._interactive_review = approve  # type: ignore[method-assign]
    monkeypatch.setattr("src.workflow.steps.asyncio.wait_for", reject_fake_timeout)
    context = WorkflowContext(
        {"entry": Entry(title="pending", source_type="generic", content="content")}
    )

    result = await step.execute(context)

    assert result["review_status"] == "approved"
    assert context.state.get("review_blocked") is False


@pytest.mark.asyncio
async def test_published_review_timeout_exception_is_not_auto_approved() -> None:
    manager = MagicMock()
    manager.create_review.return_value = 10
    step = ReviewStep(
        step_id="review",
        config={"required": True, "max_regenerations": 3, "preview_chars": 100},
        review_manager=manager,
    )

    async def timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError

    step._interactive_review = timeout  # type: ignore[method-assign]
    context = WorkflowContext(
        {"entry": Entry(title="pending", source_type="generic", content="content")}
    )

    with pytest.raises(asyncio.TimeoutError):
        await step.execute(context)

    assert context.state.get("review_blocked") is True
    manager.approve_review.assert_not_called()


@pytest.mark.asyncio
async def test_store_step_with_dummy_vector(
    tmp_path: Path,
    isolated_steps_config: Config,
) -> None:
    """StoreStep should store markdown/sqlite and call vector store."""
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "db" / "test.db"
    markdown_store = MarkdownStore(vault_dir=vault_dir)
    MigrationManager(
        db_path,
        project_root / "scripts" / "migrations",
        backup_dir=tmp_path / "backups",
    ).initialize_fresh()
    sqlite_store = SQLiteStore(db_path=db_path)
    vector_store = DummyVectorStore()
    embedder = DummyEmbedder()

    entry = Entry(
        title="Entry",
        source_type="generic",
        content="Store content",
        tags=["t1", "t2"],
    )

    context = WorkflowContext({"entry": entry})
    step = StoreStep(
        step_id="store",
        config={"targets": ["markdown", "sqlite", "vector_index"]},
        markdown_store=markdown_store,
        sqlite_store=sqlite_store,
        vector_store=vector_store,
        embedder=embedder,
    )

    result = await step.execute(context)

    assert result["file_path"]
    assert result["knowledge_id"] is not None
    assert Path(result["file_path"]).exists()
    assert vector_store.doc_calls
    assert vector_store.chunk_calls
    chunk_rows = sqlite_store.get_chunks_by_knowledge_id(result["knowledge_id"])
    assert [row["chunk_text"] for row in chunk_rows] == ["chunk1", "chunk2"]


@pytest.mark.asyncio
async def test_store_step_preserves_degraded_terminal_codes_without_retry_error(
    tmp_path: Path,
    isolated_steps_config: Config,
) -> None:
    """向量失败不否定已提交核心存储，并保留稳定错误码。"""
    db_path = tmp_path / "degraded" / "db" / "test.db"
    markdown_store = MarkdownStore(tmp_path / "degraded" / "vault")
    MigrationManager(
        db_path,
        project_root / "scripts" / "migrations",
        backup_dir=tmp_path / "degraded" / "backups",
    ).initialize_fresh()
    sqlite_store = SQLiteStore(db_path)
    vector_store = DummyVectorStore()

    def fail_vector(*_args, **_kwargs):
        raise OSError("vector unavailable")

    vector_store.add_doc_vector = fail_vector
    entry = Entry(
        title="Degraded entry",
        source_type="generic",
        content="Core content remains durable",
    )
    step = StoreStep(
        step_id="store",
        config={"targets": ["markdown", "sqlite", "vector_index"]},
        markdown_store=markdown_store,
        sqlite_store=sqlite_store,
        vector_store=vector_store,
        embedder=DummyEmbedder(),
    )

    result = await step.execute(WorkflowContext({"entry": entry}))

    assert result["status"] == "degraded"
    assert "errors" not in result
    assert result["warnings"]
    assert result["storage_errors"][0]["code"] == "storage_vector_failed"
    assert sqlite_store.query_by_id(result["knowledge_id"]) is not None
    assert result["core_committed"] is True
    assert result["do_not_retry"] is True


@pytest.mark.asyncio
async def test_store_step_terminal_journal_failure_keeps_knowledge_id_and_do_not_retry(
    tmp_path: Path,
    isolated_steps_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed core + terminal journal failure: knowledge_id retained as a
    committed-needs-repair warning, not a retry-safe generic failure."""
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "db" / "test.db"
    markdown_store = MarkdownStore(vault_dir=vault_dir)
    MigrationManager(
        db_path,
        project_root / "scripts" / "migrations",
        backup_dir=tmp_path / "backups",
    ).initialize_fresh()
    sqlite_store = SQLiteStore(db_path=db_path)
    real_coordinator = StorageCoordinator(
        markdown_store,
        sqlite_store,
        tmp_path / "ops",
    )
    original_write = real_coordinator.journal.write
    write_count = 0

    def fail_terminal_write(operation_id: str, payload: dict):
        nonlocal write_count
        write_count += 1
        if write_count == 4:
            raise OSError("terminal journal write failed")
        return original_write(operation_id, payload)

    monkeypatch.setattr(real_coordinator.journal, "write", fail_terminal_write)
    monkeypatch.setattr(
        workflow_steps,
        "StorageCoordinator",
        lambda _markdown, _sqlite, _journal_dir: real_coordinator,
    )

    entry = Entry(
        title="Committed",
        source_type="generic",
        content="content",
        tags=["t1"],
    )
    step = StoreStep(
        step_id="store",
        config={"targets": ["markdown", "sqlite"]},
        markdown_store=markdown_store,
        sqlite_store=sqlite_store,
    )

    result = await step.execute(WorkflowContext({"entry": entry}))

    # knowledge_id is retained and the row is really committed
    assert result["knowledge_id"] is not None
    assert sqlite_store.query_by_id(result["knowledge_id"]) is not None
    assert result["status"] == "repair_required"
    assert result["core_committed"] is True
    assert result["retry_safe"] is False
    assert result["do_not_retry"] is True
    assert result["operation_id"]
    assert any("do_not_retry" in error for error in result["errors"])
    assert any("do_not_retry" in warning for warning in result["warnings"])
    assert result["storage_errors"][0]["code"] == "storage_repair_required"


@pytest.mark.asyncio
async def test_store_step_missing_entry() -> None:
    """StoreStep should return errors when entry missing."""
    context = WorkflowContext({})
    step = StoreStep(step_id="store", config={})
    result = await step.execute(context)

    assert "errors" in result
    assert "缺少 Entry" in result["errors"][0]


@pytest.mark.asyncio
async def test_store_step_vector_without_sqlite(
    tmp_path: Path,
    isolated_steps_config: Config,
) -> None:
    """StoreStep should report missing knowledge_id for vector index."""
    entry = Entry(
        title="Entry",
        source_type="generic",
        content="Store content",
        tags=["t1"],
    )
    context = WorkflowContext({"entry": entry})
    step = StoreStep(
        step_id="store",
        config={"targets": ["vector_index"]},
        vector_store=DummyVectorStore(),
        embedder=DummyEmbedder(),
    )

    result = await step.execute(context)
    assert "errors" in result
    assert any("缺少 knowledge_id" in err for err in result["errors"])
