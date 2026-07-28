"""
关系服务额外白盒覆盖测试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.relations.evidence_service import EvidenceCollectionService  # noqa: E402
from src.relations.exploration_service import ExplorationService  # noqa: E402
from src.relations.models import CollectedEvidenceItem, RelationExplanationResult, TimelinePoint  # noqa: E402
from src.retrieval.result import SearchResult  # noqa: E402
from src.storage.markdown_store import Entry  # noqa: E402


class StubQueryRouter:
    def __init__(self, results):
        self.results = results

    def search(self, query: str, limit: int = 10):
        return self.results[:limit]


class StubSQLiteStore:
    def __init__(self, entries):
        self.entries = entries

    def query_by_id(self, knowledge_id: int):
        return self.entries.get(knowledge_id)


class StubMarkdownStore:
    def __init__(self, content_map=None, should_fail: bool = False):
        self.content_map = {
            Path(file_path): content for file_path, content in (content_map or {}).items()
        }
        self.should_fail = should_fail

    def load(self, file_path: Path):
        if self.should_fail:
            raise RuntimeError("load failed")
        return Entry(
            title=file_path.stem,
            source_type="generic",
            content=self.content_map[file_path],
        )


class StubRelationQueryService:
    def explain_relation(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        max_depth: int = 2,
        per_node_limit: int = 100,
    ):
        return RelationExplanationResult(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            found=False,
            explanation_type="none",
            hops=0,
            summary="",
        )

    def query_subgraph(
        self,
        seed_knowledge_id: int,
        depth: int = 2,
        per_node_limit: int = 100,
        max_nodes: int = 100,
        max_edges: int = 300,
        group_by_relation_type: bool = False,
    ):
        class Result:
            nodes = []
            edges = []

        return Result()


class StubChunkSearcher:
    def __init__(self, results):
        self.results = results

    def search_chunks(self, query: str, limit: int = 10):
        return self.results[:limit]


def _make_item(
    knowledge_id: int,
    *,
    retrieval_score: float,
    is_seed: bool = False,
    chunk_index: int | None = None,
    content_preview: str = "",
    chunk_text: str = "",
    archived_at: str = "2026-03-10 10:00:00",
) -> CollectedEvidenceItem:
    return CollectedEvidenceItem(
        knowledge_id=knowledge_id,
        title=f"Doc-{knowledge_id}",
        abstract=f"Abstract-{knowledge_id}",
        content_preview=content_preview,
        chunk_text=chunk_text,
        archived_at=archived_at,
        retrieval_rank=1,
        retrieval_score=retrieval_score,
        is_seed=is_seed,
        chunk_index=chunk_index,
    )


def test_collect_evidence_appends_document_only_results_after_chunk_hits() -> None:
    service = EvidenceCollectionService(
        query_router=StubQueryRouter(
            [
                SearchResult(1, "Alpha", 0.95, "Alpha 摘要", metadata={}),
                SearchResult(2, "Beta", 0.80, "Beta 摘要", metadata={}),
            ]
        ),
        sqlite_store=StubSQLiteStore(
            {
                1: {
                    "knowledge_id": 1,
                    "title": "Alpha",
                    "summary_one_sentence": "Alpha summary",
                    "source_type": "generic",
                    "archived_at": "2026-03-10 10:00:00",
                    "file_path": "/tmp/alpha.md",
                },
                2: {
                    "knowledge_id": 2,
                    "title": "Beta",
                    "summary_one_sentence": "Beta summary",
                    "source_type": "generic",
                    "archived_at": "2026-03-09 10:00:00",
                    "file_path": "/tmp/beta.md",
                },
            }
        ),
        markdown_store=StubMarkdownStore(
            {
                "/tmp/alpha.md": "# Alpha\n\nAlpha content",
                "/tmp/beta.md": "# Beta\n\nBeta content",
            }
        ),
        relation_query_service=StubRelationQueryService(),
        chunk_searcher=StubChunkSearcher(
            [
                SearchResult(
                    1,
                    "Alpha",
                    0.96,
                    "Alpha chunk",
                    metadata={"chunk_index": 0, "chunk_text": "Alpha chunk"},
                )
            ]
        ),
    )

    result = service.collect_evidence(
        question="Alpha 和 Beta",
        top_k=2,
        include_chunks=True,
    )

    assert [item.knowledge_id for item in result.evidence] == [1, 2]
    assert result.evidence[1].content_preview.startswith("# Beta")


def test_evidence_helper_paths_cover_dedup_trim_tokenize_and_fallbacks() -> None:
    service = EvidenceCollectionService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        markdown_store=StubMarkdownStore({}, should_fail=True),
        relation_query_service=StubRelationQueryService(),
    )

    seed_item = _make_item(1, retrieval_score=0.9, is_seed=True)
    low_score = _make_item(
        2,
        retrieval_score=0.4,
        chunk_index=0,
        chunk_text="alpha knowledge graph",
    )
    high_score = _make_item(
        2,
        retrieval_score=0.8,
        chunk_index=0,
        chunk_text="alpha knowledge graph",
    )
    exact_duplicate = _make_item(
        3,
        retrieval_score=0.6,
        content_preview="same text",
    )
    exact_duplicate_candidate = _make_item(
        4,
        retrieval_score=0.7,
        content_preview="same text",
    )
    near_duplicate_candidate = _make_item(
        5,
        retrieval_score=0.7,
        content_preview="same text with retrieval signal.",
    )
    exact_duplicate_long = _make_item(
        6,
        retrieval_score=0.6,
        content_preview="same text with retrieval signal",
    )

    deduplicated = service._deduplicate_evidence_items([low_score, high_score])
    assert deduplicated[0].retrieval_score == 0.8
    assert service._find_duplicate_index([exact_duplicate], exact_duplicate_candidate) == 0
    assert service._find_duplicate_index([exact_duplicate_long], near_duplicate_candidate) == 0
    assert service._rank_evidence_items("question", []) == []
    assert service._trim_evidence_items([seed_item, high_score, near_duplicate_candidate], 2) == [
        seed_item,
        high_score,
    ]
    assert service._compute_coverage_score("", high_score) == 0.0
    assert service._compute_coverage_score(
        "question",
        _make_item(6, retrieval_score=0.5, content_preview=""),
    ) == 0.0
    assert service._compute_freshness_score(high_score, None) == 0.0
    invalid_freshness = service._compute_freshness_score(
        _make_item(7, retrieval_score=0.5, archived_at="not-a-time"),
        service._parse_timestamp("2026-03-11 10:00:00"),
    )
    assert invalid_freshness == 0.0
    service.text_processor.tokenize_chinese = lambda text: "   "  # type: ignore[method-assign]
    assert "alpha" in service._tokenize_text("Alpha 图谱")
    assert service._load_content_preview("") == ""
    assert service._load_content_preview("/tmp/missing.md") == ""
    assert service._parse_tags(["A", " ", "B"]) == ["A", "B"]
    assert service._parse_tags("A, B") == ["A", "B"]


def test_exploration_helper_paths_cover_validation_and_fallbacks() -> None:
    service = ExplorationService(
        query_router=StubQueryRouter([]),
        sqlite_store=StubSQLiteStore({}),
        relation_query_service=StubRelationQueryService(),
    )

    with pytest.raises(ValueError):
        service.find_bridges(seed_knowledge_id=0)
    with pytest.raises(ValueError):
        service.find_bridges(seed_knowledge_id=1, top_k=0)
    with pytest.raises(ValueError):
        service.timeline_of("topic", sort_order="invalid")
    with pytest.raises(ValueError):
        service.contrast("", "topic-b")
    with pytest.raises(ValueError):
        service.contrast("topic-a", " ", top_k=1)

    assert service._parse_tags(["A", " ", "B"]) == ["A", "B"]
    assert service._token_overlap(set(), {"x"}) == 0.0
    assert service._entry_tokens({}) == set()

    service.text_processor.tokenize_chinese = lambda text: "   "  # type: ignore[method-assign]
    assert "alpha" in service._entry_tokens({"title": "Alpha 图谱"})
    empty_bridge_score = service._compute_semantic_bridge_score(
        {}, {"title": ""}, {2}, {}
    )
    assert empty_bridge_score == 0.0
    missing_bridge_score = service._compute_semantic_bridge_score(
        {},
        {"title": "Alpha"},
        {2},
        {2: {}},
    )
    assert missing_bridge_score == 0.0
    assert service._infer_timeline_source([], []) == "archived_at"
    inferred_source = service._infer_timeline_source(
        [
            TimelinePoint(
                knowledge_id=1,
                title="NoTime",
                time_value="",
                time_source="event_time",
                retrieval_score=0.1,
            )
        ],
        ["event_time", "published_at", "archived_at"],
    )
    assert inferred_source == "archived_at"
    assert service._parse_time_sort_key("2026-03-01T10:00:00Z")[:2] == (0, 0)

    explanation = RelationExplanationResult(
        source_knowledge_id=1,
        target_knowledge_id=2,
        found=True,
        explanation_type="fallback",
        hops=1,
        summary="",
        evidence_items=[{"relation_type": "references"}],
    )
    assert service._extract_relation_types(explanation) == ["references"]
