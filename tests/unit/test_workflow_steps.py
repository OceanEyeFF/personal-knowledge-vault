"""
Workflow steps unit tests.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pytest

from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from src.workflow.models import WorkflowContext
from src.utils.config import Config
import src.workflow.steps as workflow_steps
from src.workflow.steps import FetchStep, AnalyzeStep, IdeaSharpenStep, StoreStep


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

    monkeypatch.setattr("src.workflow.steps.get_processor", get_failing_processor_stub)
    monkeypatch.setattr("src.workflow.steps.asyncio.sleep", fast_sleep)

    context = WorkflowContext({"url": "https://example.com"})
    step = FetchStep(step_id="fetch", config={"retry": 1, "processor": "wechat"})
    result = await step.execute(context)

    assert "errors" in result
    assert "抓取失败" in result["errors"][0]


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
    step = AnalyzeStep(step_id="analyze", config={"tasks": ["summarize", "extract_tags"]}, deepseek_client=DummyDeepSeekClient())

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

    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={
            "questions": ["Q1", "Q2"],
            "condition": "content_length > 0",
            "timeout": 1,
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


@pytest.mark.asyncio
async def test_idea_sharpen_step_no_questions() -> None:
    """IdeaSharpenStep should skip when no questions provided."""
    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(step_id="sharpen", config={"questions": []})

    result = await step.execute(context)
    assert result == {}


@pytest.mark.asyncio
async def test_idea_sharpen_step_condition_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """IdeaSharpenStep should skip on condition parse error."""
    monkeypatch.setattr("src.workflow.steps.Prompt.ask", prompt_stub)

    context = WorkflowContext({"content_length": 10})
    step = IdeaSharpenStep(
        step_id="sharpen",
        config={"questions": ["Q1"], "condition": "invalid =="},
    )

    result = await step.execute(context)
    assert result == {}


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
async def test_store_step_with_dummy_vector(
    tmp_path: Path,
    isolated_steps_config: Config,
) -> None:
    """StoreStep should store markdown/sqlite and call vector store."""
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "db" / "test.db"
    markdown_store = MarkdownStore(vault_dir=vault_dir)
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
