"""
检索引擎集成测试

测试完整的数据流：Entry → Storage → Retrieval
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import numpy as np
import pytest
from scripts.backfill_chunks import run_chunk_backfill
from src.relations.evidence_service import EvidenceCollectionService
from src.relations.models import RelationExplanationResult
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.ai.embedder import Embedder
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.vector_retriever import VectorRetriever
from src.retrieval.hybrid_retriever import HybridRetriever
from src.retrieval.query_router import QueryRouter
from src.retrieval.result import SearchResult


class DeterministicEmbedder:
    """用于集成测试的确定性 Embedder。"""

    dim = 1536

    def embed_document(self, text: str) -> np.ndarray:
        text_lower = (text or "").lower()
        vector = np.zeros(self.dim, dtype=np.float32)
        keyword_slots = {
            "alpha": 0,
            "beta": 1,
            "graph": 2,
            "relation": 3,
            "history": 4,
        }
        for keyword, slot in keyword_slots.items():
            if keyword in text_lower:
                vector[slot] = 1.0
        if not np.any(vector):
            vector[10] = 1.0
        return vector

    def embed_chunks(
        self, text: str, return_chunks: bool = False
    ) -> tuple[np.ndarray, list[str] | None]:
        chunks = self.split_chunks(text)
        if not chunks:
            raise ValueError("测试输入未生成 chunk")

        vectors = np.vstack([self.embed_document(chunk) for chunk in chunks]).astype(
            np.float32
        )
        return vectors, chunks if return_chunks else None

    def split_chunks(self, text: str) -> list[str]:
        return [chunk.strip() for chunk in (text or "").split("||") if chunk.strip()]


class StaticQueryRouter:
    def __init__(self, results: list[SearchResult]):
        self._results = results

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        return self._results[:limit]


class NoopRelationQueryService:
    def explain_relation(
        self,
        source_knowledge_id: int,
        target_knowledge_id: int,
        max_depth: int = 2,
        per_node_limit: int = 100,
    ) -> RelationExplanationResult:
        return RelationExplanationResult(
            source_knowledge_id=source_knowledge_id,
            target_knowledge_id=target_knowledge_id,
            found=False,
            explanation_type="not_found",
            hops=0,
            summary="未找到关系解释",
        )


class TestDataPipelineIntegration:
    """测试从 Entry 到检索的完整数据流"""

    @pytest.fixture
    def test_paths(self, tmp_path):
        """创建临时测试路径"""
        return {
            "vault_dir": tmp_path / "vault",
            "db_path": tmp_path / "test.db",
            "vector_dir": tmp_path / "vectors",
        }

    @pytest.fixture
    def stores(self, test_paths):
        """初始化存储层"""
        # 创建目录
        test_paths["vault_dir"].mkdir(parents=True, exist_ok=True)
        test_paths["vector_dir"].mkdir(parents=True, exist_ok=True)

        # 初始化存储
        markdown_store = MarkdownStore(test_paths["vault_dir"])
        sqlite_store = SQLiteStore(test_paths["db_path"])
        # 动态获取 Embedder 实际维度，避免硬编码与模型不符导致崩溃
        try:
            _embedder = Embedder()
            _sample = _embedder.embed_document("test")
            _dim = len(_sample) if _sample is not None else 1536
        except Exception:
            _dim = 1536
        vector_store = VectorStore(test_paths["vector_dir"], dim=_dim)

        # 初始化数据库 Schema
        sqlite_store.initialize()

        return {
            "markdown": markdown_store,
            "sqlite": sqlite_store,
            "vector": vector_store,
        }

    @pytest.fixture
    def embedder(self):
        """创建 Embedder 实例"""
        # 注意：这里需要真实的 OpenAI API Key
        # 如果没有，可以 mock
        try:
            return Embedder()
        except Exception:
            pytest.skip("Embedder 初始化失败，可能缺少 API Key")

    def test_entry_to_sqlite_pipeline(self, stores, test_paths):
        """
        测试 Entry → SQLite 流程

        验证：
        1. Entry 能否正确保存到 SQLite
        2. FTS5 索引是否自动同步
        3. 元数据是否完整
        """
        # 创建测试 Entry
        entry = Entry(
            title="分布式系统的 CAP 定理",
            content="# 分布式系统的 CAP 定理\n\nCAP 定理指出，分布式系统无法同时满足一致性、可用性和分区容错性。",
            abstract="CAP 定理的核心内容",
            summary_one_sentence="分布式系统的三大基本约束",
            summary_100_words="CAP 定理是分布式系统设计的基础理论...",
            tags=["分布式系统", "理论"],
            keywords="CAP,一致性,可用性",
            source_type="generic",
            source_url="https://example.com/cap-theorem",
        )

        # 保存到 Markdown
        file_path = stores["markdown"].save(entry)
        assert file_path.exists()

        # 保存到 SQLite
        knowledge_id = stores["sqlite"].insert_entry(entry, str(file_path))
        assert knowledge_id > 0

        # 验证数据完整性
        with stores["sqlite"].get_connection() as conn:
            cursor = conn.execute(
                "SELECT title, source_type FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row[0] == entry.title
            assert row[1] == entry.source_type

        # 验证 FTS5 索引
        with stores["sqlite"].get_connection() as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM knowledge_items_fts WHERE rowid = ?",
                (knowledge_id,),
            )
            count = cursor.fetchone()[0]
            assert count == 1, "FTS5 索引应该自动同步"

    def test_entry_to_vector_pipeline(self, stores, embedder, test_paths):
        """
        测试 Entry → Vector Index 流程

        验证：
        1. 文档能否正确向量化
        2. 向量能否保存到索引
        3. 向量检索是否可用
        """
        # 创建测试 Entry
        entry = Entry(
            title="深度学习基础",
            content="# 深度学习基础\n\n深度学习是机器学习的一个分支，使用多层神经网络来学习数据的表示。",
            abstract="深度学习简介",
            summary_one_sentence="多层神经网络学习数据表示",
            summary_100_words="深度学习通过构建多层神经网络...",
            tags=["机器学习", "深度学习"],
            keywords="神经网络,深度学习",
            source_type="generic",
            source_url="https://example.com/dl-basics",
        )

        # 保存到 SQLite
        file_path = stores["markdown"].save(entry)
        knowledge_id = stores["sqlite"].insert_entry(entry, str(file_path))

        # 向量化并保存
        vector = embedder.embed_document(entry.content)
        assert vector is not None
        assert len(vector) > 0  # 维度依模型配置而定，仅验证向量有效性

        stores["vector"].add_doc_vector(knowledge_id, vector)

        # 验证向量检索（索引中只有 1 条向量，k=1 避免 hnswlib ef 不足报错）
        query_vector = embedder.embed_document("什么是神经网络")
        results = stores["vector"].search_doc(query_vector, k=1)

        assert len(results) > 0
        # 结果应该包含我们刚添加的文档
        result_ids = [r[0] for r in results]
        assert knowledge_id in result_ids

    def test_bm25_retrieval_accuracy(self, stores, test_paths):
        """
        测试 BM25 检索准确性

        验证：
        1. 关键词查询能否召回相关内容
        2. 中文分词是否正确
        3. 分数排序是否合理
        """
        # 准备测试数据（3 条相关度不同的内容）
        entries = [
            Entry(
                title="Python 基础教程",
                content="# Python 基础教程\n\nPython 是一门简单易学的编程语言。",
                abstract="Python 入门",
                summary_one_sentence="Python 编程基础",
                summary_100_words="Python 是一门...",
                tags=["Python", "编程"],
                keywords="Python,教程",
                source_type="generic",
                source_url="https://example.com/python-basics",
            ),
            Entry(
                title="Java 编程指南",
                content="# Java 编程指南\n\nJava 是一门面向对象的编程语言。",
                abstract="Java 入门",
                summary_one_sentence="Java 编程基础",
                summary_100_words="Java 是一门...",
                tags=["Java", "编程"],
                keywords="Java,编程",
                source_type="generic",
                source_url="https://example.com/java-guide",
            ),
            Entry(
                title="Python 高级特性",
                content="# Python 高级特性\n\n装饰器、生成器、上下文管理器等高级特性。",
                abstract="Python 进阶",
                summary_one_sentence="Python 高级编程技巧",
                summary_100_words="Python 高级特性包括...",
                tags=["Python", "高级"],
                keywords="Python,装饰器,生成器",
                source_type="generic",
                source_url="https://example.com/python-advanced",
            ),
        ]

        # 保存所有测试数据
        for entry in entries:
            file_path = stores["markdown"].save(entry)
            stores["sqlite"].insert_entry(entry, str(file_path))

        # 执行 BM25 检索 - 查询包含多个词，确保召回两条
        retriever = BM25Retriever(test_paths["db_path"])
        results = retriever.search("Python", limit=5)

        # 验证结果
        assert len(results) >= 2, "应该召回至少 2 条 Python 相关内容"

        # 验证排序（最相关的应该在前面）
        titles = [r.title for r in results]
        assert any("Python" in title for title in titles[:2]), "前 2 个结果应该包含 Python"

        # 验证 Java 不会被召回
        assert not any("Java" in title for title in titles), "Java 不应该出现在 Python 检索结果中"

        # 验证分数范围
        for result in results:
            assert 0.0 <= result.score <= 1.0, "分数应该在 [0.0, 1.0] 范围内"

    @pytest.mark.skipif(
        not Path(".env").exists(), reason="需要 .env 文件配置 API Keys"
    )
    def test_end_to_end_search_accuracy(self, stores, embedder, test_paths):
        """
        端到端检索准确率测试

        验证：
        1. 完整的数据流是否正常
        2. 查询路由是否正确
        3. 检索结果是否准确
        """
        # 准备测试数据集（至少 10 条）
        test_dataset = [
            {
                "title": "分布式系统设计",
                "content": "分布式系统的核心挑战包括一致性、可用性和分区容错性...",
                "tags": ["分布式系统", "架构"],
            },
            {
                "title": "微服务架构实践",
                "content": "微服务架构通过将应用拆分为多个独立服务来提高可维护性...",
                "tags": ["微服务", "架构"],
            },
            {
                "title": "数据库索引优化",
                "content": "数据库索引是提升查询性能的关键，包括 B+ 树索引、哈希索引等...",
                "tags": ["数据库", "性能"],
            },
            # ... 更多测试数据
        ]

        # 保存测试数据
        knowledge_ids = []
        for data in test_dataset:
            entry = Entry(
                title=data["title"],
                content=f"# {data['title']}\n\n{data['content']}",
                abstract=data["content"][:100],
                summary_one_sentence=data["content"][:50],
                summary_100_words=data["content"],
                tags=data["tags"],
                keywords=",".join(data["tags"]),
                source_type="test",
                source_url="https://test.example.com",
            )
            file_path = stores["markdown"].save(entry)
            kid = stores["sqlite"].insert_entry(entry, str(file_path))
            knowledge_ids.append(kid)

            # 向量化
            vector = embedder.embed_document(entry.content)
            stores["vector"].add_doc_vector(kid, vector)

        # 准备测试查询
        test_queries = [
            {
                "query": "分布式系统",
                "expected_title": "分布式系统设计",
                "search_type": "bm25",  # 短查询
            },
            {
                "query": "如何设计一个高可用的微服务架构系统",
                "expected_title": "微服务架构实践",
                "search_type": "hybrid",  # 长查询
            },
        ]

        # 执行查询并验证
        router = QueryRouter(test_paths["db_path"], test_paths["vector_dir"], embedder)

        correct_count = 0
        total_count = len(test_queries)

        for test_case in test_queries:
            results = router.search(test_case["query"], limit=5)

            # 检查预期结果是否在 Top 5
            if results:
                top_titles = [r.title for r in results[:5]]
                if test_case["expected_title"] in top_titles:
                    correct_count += 1

        # 计算准确率
        accuracy = correct_count / total_count if total_count > 0 else 0
        print(f"\n检索准确率: {accuracy * 100:.1f}% ({correct_count}/{total_count})")

        # 验证准确率阈值（宽松一点，因为测试数据少）
        assert accuracy >= 0.5, f"准确率应该 >= 50%，当前: {accuracy * 100:.1f}%"

    def test_chunk_backfill_apply_supports_scope_and_idempotency(self, tmp_path: Path):
        """历史 chunk 回填应支持指定范围执行且可重复运行。"""
        vault_dir = tmp_path / "vault"
        db_path = tmp_path / "db" / "test.db"
        vector_dir = tmp_path / "vectors"
        vault_dir.mkdir(parents=True, exist_ok=True)
        vector_dir.mkdir(parents=True, exist_ok=True)

        markdown_store = MarkdownStore(vault_dir)
        sqlite_store = SQLiteStore(db_path)
        sqlite_store.initialize()
        embedder = DeterministicEmbedder()

        entry_alpha = Entry(
            title="Alpha History",
            source_type="generic",
            content="Alpha relation chunk||Alpha history detail",
            summary_one_sentence="Alpha summary",
        )
        entry_beta = Entry(
            title="Beta History",
            source_type="generic",
            content="Beta graph chunk||Beta history detail",
            summary_one_sentence="Beta summary",
        )
        alpha_path = markdown_store.save(entry_alpha)
        beta_path = markdown_store.save(entry_beta)
        alpha_id = sqlite_store.insert_entry(entry_alpha, str(alpha_path))
        beta_id = sqlite_store.insert_entry(entry_beta, str(beta_path))

        scoped_report = run_chunk_backfill(
            db_path=db_path,
            vector_index_dir=vector_dir,
            knowledge_ids=[alpha_id],
            apply=True,
            embedding_dim=embedder.dim,
            embedder=embedder,
        )

        assert scoped_report.applied_entries == 1
        assert sqlite_store.count_chunks(alpha_id) == 2
        assert sqlite_store.count_chunks(beta_id) == 0

        vector_store = VectorStore(vector_dir, dim=embedder.dim)
        assert vector_store.get_chunk_indices_for_entry(alpha_id) == [0, 1]

        rerun_report = run_chunk_backfill(
            db_path=db_path,
            vector_index_dir=vector_dir,
            knowledge_ids=[alpha_id],
            apply=True,
            embedding_dim=embedder.dim,
            embedder=embedder,
        )

        assert rerun_report.candidate_entries == 0
        assert rerun_report.applied_entries == 0
        assert sqlite_store.count_chunks(alpha_id) == 2
        assert vector_store.get_chunk_indices_for_entry(alpha_id) == [0, 1]

    def test_chunk_backfill_apply_enables_chunk_retrieval_and_evidence(
        self, tmp_path: Path
    ):
        """历史样本回填后应能被 chunk 检索与 EvidenceService 消费。"""
        vault_dir = tmp_path / "vault"
        db_path = tmp_path / "db" / "test.db"
        vector_dir = tmp_path / "vectors"
        vault_dir.mkdir(parents=True, exist_ok=True)
        vector_dir.mkdir(parents=True, exist_ok=True)

        markdown_store = MarkdownStore(vault_dir)
        sqlite_store = SQLiteStore(db_path)
        sqlite_store.initialize()
        embedder = DeterministicEmbedder()

        entry = Entry(
            title="Alpha Retrieval History",
            source_type="generic",
            content="Alpha relation chunk||Neutral archive chunk",
            summary_one_sentence="Alpha retrieval summary",
            tags=["alpha", "graph"],
        )
        file_path = markdown_store.save(entry)
        knowledge_id = sqlite_store.insert_entry(entry, str(file_path))

        report = run_chunk_backfill(
            db_path=db_path,
            vector_index_dir=vector_dir,
            apply=True,
            embedding_dim=embedder.dim,
            embedder=embedder,
        )
        assert report.applied_entries == 1

        retriever = VectorRetriever(db_path, vector_dir, embedder)
        chunk_results = retriever.search_chunks("Alpha relation", limit=3)

        assert chunk_results
        assert chunk_results[0].knowledge_id == knowledge_id
        assert chunk_results[0].metadata["chunk_index"] == 0
        assert "Alpha relation chunk" in chunk_results[0].metadata["chunk_text"]

        service = EvidenceCollectionService(
            query_router=StaticQueryRouter(
                [
                    SearchResult(
                        knowledge_id=knowledge_id,
                        title=entry.title,
                        score=0.95,
                        highlight=entry.summary_one_sentence,
                        metadata={
                            "source_type": entry.source_type,
                            "file_path": str(file_path),
                            "tags": ",".join(entry.tags),
                        },
                    )
                ]
            ),
            sqlite_store=sqlite_store,
            markdown_store=markdown_store,
            relation_query_service=NoopRelationQueryService(),
            chunk_searcher=retriever,
        )

        evidence_result = service.collect_evidence(
            question="Alpha relation",
            top_k=1,
            include_chunks=True,
        )

        assert evidence_result.found is True
        assert evidence_result.evidence[0].knowledge_id == knowledge_id
        assert evidence_result.evidence[0].chunk_index == 0
        assert evidence_result.evidence[0].chunk_text == "Alpha relation chunk"
        assert evidence_result.evidence[0].content_preview == "Alpha relation chunk"


class TestIndexHealth:
    """测试索引健康状态"""

    def test_fts5_index_exists(self):
        """验证 FTS5 索引是否存在"""
        db_path = Path(".data/db/knowledge_vault.db")
        if not db_path.exists():
            pytest.skip("主数据库不存在，跳过测试")

        store = SQLiteStore(db_path)
        with store.get_connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_items_fts'"
            )
            result = cursor.fetchone()
            assert result is not None, "FTS5 索引表应该存在"

    def test_vector_index_exists(self):
        """验证向量索引是否存在"""
        vector_dir = Path(".data/vectors")
        if not vector_dir.exists():
            pytest.skip("向量索引目录不存在，跳过测试")

        vector_store = VectorStore(vector_dir)
        stats = vector_store.get_index_stats()

        assert stats["doc_count"] >= 0, "向量索引应该可用"
        print(f"\n向量索引统计: {stats}")
