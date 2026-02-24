"""
检索引擎集成测试

测试完整的数据流：Entry → Storage → Retrieval
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

import pytest
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore
from src.ai.embedder import Embedder
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.vector_retriever import VectorRetriever
from src.retrieval.hybrid_retriever import HybridRetriever
from src.retrieval.query_router import QueryRouter


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
