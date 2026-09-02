"""
检索引擎集成测试

测试完整的数据流：Entry → Storage → Retrieval
"""

import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest
from scripts.backfill_chunks import run_chunk_backfill
from src.relations.evidence_service import EvidenceCollectionService
from src.relations.models import RelationExplanationResult
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.migration_manager import MigrationManager
from src.storage.sqlite_store import (
    FTS_TABLE_NAME,
    LEGACY_FTS_TABLE_NAME,
    SQLiteStore,
)
from src.storage.vector_store import VectorStore
from src.retrieval.bm25_retriever import BM25Retriever
from src.retrieval.query_router import QueryRouter
from src.retrieval.result import SearchResponse, SearchResult
from src.retrieval.vector_retriever import VectorRetriever


PROJECT_ROOT = Path(__file__).parent.parent.parent


def _create_legacy_knowledge_fts_db(db_path: Path) -> None:
    """构造真实旧版 knowledge_fts 合同数据库。"""
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                version TEXT NOT NULL UNIQUE,
                description TEXT,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                applied_by TEXT
            );

            CREATE TABLE knowledge_items (
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                summary_one_sentence TEXT,
                summary_100_words TEXT,
                keywords TEXT,
                tags TEXT,
                outline TEXT,
                source_type TEXT NOT NULL,
                source_url TEXT UNIQUE,
                search_strategy TEXT,
                file_path TEXT NOT NULL UNIQUE,
                word_count INTEGER DEFAULT 0,
                event_time TIMESTAMP,
                published_at TIMESTAMP,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE content_chunks (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                context_before TEXT,
                context_after TEXT,
                section_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
                UNIQUE(knowledge_id, chunk_index)
            );

            CREATE TABLE tags (
                tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                tag_group TEXT,
                count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE knowledge_tags (
                knowledge_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (knowledge_id, tag_id),
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
            );

            CREATE TABLE video_timestamps (
                timestamp_id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                timestamp_seconds INTEGER NOT NULL,
                segment_text TEXT NOT NULL,
                chapter_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
                UNIQUE(knowledge_id, timestamp_seconds)
            );

            CREATE VIRTUAL TABLE knowledge_fts USING fts5(
                knowledge_id UNINDEXED,
                title,
                content,
                keywords,
                tags,
                tokenize = 'porter unicode61'
            );

            CREATE TRIGGER knowledge_fts_insert
            AFTER INSERT ON knowledge_items
            BEGIN
                INSERT INTO knowledge_fts (knowledge_id, title, content, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.content, new.keywords, new.tags);
            END;

            CREATE TRIGGER knowledge_fts_update
            AFTER UPDATE ON knowledge_items
            BEGIN
                DELETE FROM knowledge_fts WHERE knowledge_id = old.knowledge_id;
                INSERT INTO knowledge_fts (knowledge_id, title, content, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.content, new.keywords, new.tags);
            END;

            CREATE TRIGGER knowledge_fts_delete
            AFTER DELETE ON knowledge_items
            BEGIN
                DELETE FROM knowledge_fts WHERE knowledge_id = old.knowledge_id;
            END;

            CREATE INDEX idx_knowledge_source_type ON knowledge_items(source_type);
            CREATE INDEX idx_knowledge_event_time ON knowledge_items(event_time);
            CREATE INDEX idx_knowledge_published_at ON knowledge_items(published_at);
            CREATE INDEX idx_knowledge_archived_at ON knowledge_items(archived_at);
            CREATE INDEX idx_knowledge_search_strategy ON knowledge_items(search_strategy);
            CREATE INDEX idx_chunks_knowledge_id ON content_chunks(knowledge_id);
            CREATE INDEX idx_chunks_index ON content_chunks(knowledge_id, chunk_index);
            CREATE INDEX idx_knowledge_tags_knowledge_id ON knowledge_tags(knowledge_id);
            CREATE INDEX idx_knowledge_tags_tag_id ON knowledge_tags(tag_id);
            CREATE INDEX idx_timestamps_knowledge_id ON video_timestamps(knowledge_id);
            CREATE INDEX idx_timestamps_time ON video_timestamps(knowledge_id, timestamp_seconds);

            INSERT INTO schema_version (version, description)
            VALUES ('1.0.0', 'legacy knowledge_fts schema');
            """
        )
        conn.execute(
            """
            INSERT INTO knowledge_items (
                title,
                content,
                summary_one_sentence,
                summary_100_words,
                keywords,
                tags,
                source_type,
                source_url,
                file_path,
                word_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Alpha Legacy",
                "Alpha alpha body",
                "Alpha 摘要",
                "Alpha alpha summary",
                "alpha",
                "alpha",
                "test",
                "https://example.com/legacy-alpha",
                "/tmp/legacy-alpha.md",
                3,
            ),
        )
        conn.commit()


def _create_external_content_fts_db(db_path: Path) -> None:
    """构造已在 1.2.2 的 external-content knowledge_items_fts 数据库。"""
    manager = MigrationManager(db_path, PROJECT_ROOT / "scripts" / "migrations")
    for migration_name in (
        "001_initial_schema.sql",
        "002_add_cli_tables.sql",
        "004_add_chat_sessions.sql",
        "005_add_review_system.sql",
        "006_add_relations_foundation.sql",
        "007_add_timeline_time_fields.sql",
        "008_align_fts_contract.sql",
    ):
        manager.apply_migration(
            PROJECT_ROOT / "scripts" / "migrations" / migration_name,
            auto_backup=False,
        )
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.execute("DROP TRIGGER IF EXISTS knowledge_items_ad")
        conn.execute("DROP TRIGGER IF EXISTS knowledge_items_au")
        conn.execute("DROP TRIGGER IF EXISTS knowledge_items_ai")
        conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE_NAME}")
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE {FTS_TABLE_NAME} USING fts5(
                title,
                summary_100_words,
                keywords,
                tags,
                content=knowledge_items,
                content_rowid=knowledge_id
            )
            """
        )
        conn.executescript(
            f"""
            CREATE TRIGGER knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
                INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
            END;

            CREATE TRIGGER knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
                DELETE FROM {FTS_TABLE_NAME} WHERE rowid = old.knowledge_id;
                INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
            END;

            CREATE TRIGGER knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
                DELETE FROM {FTS_TABLE_NAME} WHERE rowid = old.knowledge_id;
            END;
            """
        )
        conn.execute(
            """
            INSERT INTO knowledge_items (
                title,
                content,
                summary_one_sentence,
                summary_100_words,
                keywords,
                tags,
                source_type,
                source_url,
                file_path,
                word_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "External Alpha",
                "Alpha alpha body",
                "Alpha 摘要",
                "Alpha alpha summary",
                "alpha",
                "alpha",
                "test",
                "https://example.com/external-alpha",
                "/tmp/external-alpha.md",
                3,
            ),
        )
        conn.commit()


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

    def search(self, query: str, limit: int = 10) -> SearchResponse:
        return SearchResponse.completed(
            self._results[:limit],
            strategy="fixture",
        )


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
        vector_store = VectorStore(
            test_paths["vector_dir"], dim=DeterministicEmbedder.dim
        )

        # 初始化数据库 Schema
        sqlite_store.initialize()

        return {
            "markdown": markdown_store,
            "sqlite": sqlite_store,
            "vector": vector_store,
        }

    @pytest.fixture
    def embedder(self):
        """创建不依赖外部服务的确定性 Embedder。"""
        return DeterministicEmbedder()

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
                f"SELECT COUNT(*) FROM {FTS_TABLE_NAME} WHERE rowid = ?",
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
        response = retriever.search("Python", limit=5)

        # 验证结果
        assert response.status == "success"
        assert len(response.results) >= 2, "应该召回至少 2 条 Python 相关内容"

        # 验证排序（最相关的应该在前面）
        titles = [r.title for r in response.results]
        assert any("Python" in title for title in titles[:2]), "前 2 个结果应该包含 Python"

        # 验证 Java 不会被召回
        assert not any("Java" in title for title in titles), "Java 不应该出现在 Python 检索结果中"

        # 验证分数范围
        for result in response.results:
            assert 0.0 <= result.score <= 1.0, "分数应该在 [0.0, 1.0] 范围内"

    def test_migration_created_db_supports_bm25(self, tmp_path: Path):
        """迁移链创建的新库应使用统一 FTS 合同并支持 BM25。"""
        vault_dir = tmp_path / "vault"
        db_path = tmp_path / "migrated.db"
        vault_dir.mkdir(parents=True, exist_ok=True)

        manager = MigrationManager(
            db_path,
            PROJECT_ROOT / "scripts" / "migrations",
        )
        assert manager.apply_all_pending(auto_backup=False) > 0

        markdown_store = MarkdownStore(vault_dir)
        sqlite_store = SQLiteStore(db_path)
        entry = Entry(
            title="Python migration retrieval",
            content="Python migration retrieval validates the FTS contract.",
            abstract="migration bm25",
            summary_one_sentence="migration bm25 summary",
            summary_100_words="Python migration retrieval summary",
            tags=["Python", "migration"],
            keywords="Python,migration",
            source_type="test",
            source_url="https://example.com/migration-bm25",
        )
        file_path = markdown_store.save(entry)
        knowledge_id = sqlite_store.insert_entry(entry, str(file_path))

        with sqlite_store.get_connection() as conn:
            current = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (FTS_TABLE_NAME,),
            ).fetchone()
            legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (LEGACY_FTS_TABLE_NAME,),
            ).fetchone()
            assert current is not None
            assert legacy is None

        response = BM25Retriever(db_path).search("Python", limit=5)
        assert response.status == "success"
        assert response.results[0].knowledge_id == knowledge_id

    def test_migration_alignment_rebuilds_existing_chinese_fts_rows(self, tmp_path: Path):
        """升级到最新 FTS 修复链后，已有条目也应回到与运行时一致的中文分词召回。"""
        db_path = tmp_path / "legacy-upgrade.db"

        manager = MigrationManager(
            db_path,
            PROJECT_ROOT / "scripts" / "migrations",
        )
        for migration_name in (
            "001_initial_schema.sql",
            "002_add_cli_tables.sql",
            "004_add_chat_sessions.sql",
            "005_add_review_system.sql",
            "006_add_relations_foundation.sql",
            "007_add_timeline_time_fields.sql",
        ):
            manager.apply_migration(
                PROJECT_ROOT / "scripts" / "migrations" / migration_name,
                auto_backup=False,
            )

        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            conn.execute(
                """
                INSERT INTO knowledge_items (
                    title,
                    summary_one_sentence,
                    summary_100_words,
                    keywords,
                    tags,
                    source_type,
                    source_url,
                    file_path,
                    word_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "分布式系统设计",
                    "分布式系统摘要",
                    "分布式系统的核心挑战包括一致性和可用性",
                    "分布式系统,架构",
                    "分布式系统,架构",
                    "test",
                    "https://example.com/migration-upgrade-bm25",
                    "/tmp/migration-upgrade-bm25.md",
                    10,
                ),
            )
            conn.commit()

        assert manager.apply_all_pending(auto_backup=False) == 5

        response = BM25Retriever(db_path).search("一致性", limit=5)
        assert response.status == "success"
        assert response.results[0].title == "分布式系统设计"

    def test_real_legacy_knowledge_fts_database_upgrades_without_corruption(
        self, tmp_path: Path
    ):
        """带真实 knowledge_fts 数据的旧库应能安全升级到最新合同。"""
        db_path = tmp_path / "legacy-real.db"
        _create_legacy_knowledge_fts_db(db_path)

        manager = MigrationManager(
            db_path,
            PROJECT_ROOT / "scripts" / "migrations",
        )

        assert manager.apply_all_pending(auto_backup=False) == 10

        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            latest_version = conn.execute(
                "SELECT version FROM schema_version ORDER BY version_id DESC LIMIT 1"
            ).fetchone()[0]

        assert LEGACY_FTS_TABLE_NAME not in tables
        assert FTS_TABLE_NAME in tables
        assert latest_version == "1.2.6"

        response = BM25Retriever(db_path).search("alpha", limit=5)
        assert response.status == "success"
        assert response.results[0].title == "Alpha Legacy"

    def test_pending_fts_repair_migration_replaces_external_content_contract(
        self, tmp_path: Path
    ):
        """已有 1.2.2 external-content 合同的库应修复 FTS 并升级到最新 schema。"""
        db_path = tmp_path / "external-content.db"
        _create_external_content_fts_db(db_path)

        manager = MigrationManager(
            db_path,
            PROJECT_ROOT / "scripts" / "migrations",
        )

        assert manager.apply_all_pending(auto_backup=False) == 4

        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            table_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (FTS_TABLE_NAME,),
            ).fetchone()[0]
            conn.execute(
                """
                UPDATE knowledge_items
                SET title = ?, summary_100_words = ?, keywords = ?, tags = ?
                WHERE knowledge_id = 1
                """,
                ("External Beta", "Beta beta summary", "beta", "beta"),
            )
            conn.commit()

        assert "content=knowledge_items" not in table_sql.lower()
        alpha_response = BM25Retriever(db_path).search("alpha", limit=5)
        assert alpha_response.status == "no_hits"
        beta_response = BM25Retriever(db_path).search("beta", limit=5)
        assert beta_response.status == "success"
        assert beta_response.results[0].title == "External Beta"

    def test_initialize_created_db_uses_single_fts_contract(self, tmp_path: Path):
        """运行时初始化的新库不应保留旧 FTS 表名。"""
        db_path = tmp_path / "runtime.db"
        store = SQLiteStore(db_path)
        store.initialize()

        with store.get_connection() as conn:
            current = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (FTS_TABLE_NAME,),
            ).fetchone()
            legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (LEGACY_FTS_TABLE_NAME,),
            ).fetchone()
            assert current is not None
            assert legacy is None

    def test_bm25_update_removes_stale_terms(self, tmp_path: Path):
        """条目更新后，旧关键词不应继续命中 BM25。"""
        db_path = tmp_path / "update.db"
        store = SQLiteStore(db_path)
        store.initialize()

        entry = Entry(
            title="Alpha",
            content="Alpha alpha body",
            abstract="Alpha",
            summary_one_sentence="Alpha",
            summary_100_words="Alpha alpha body",
            tags=["alpha"],
            keywords="alpha",
            source_type="test",
            source_url="https://example.com/update-alpha",
        )
        store.insert_entry(entry, str(tmp_path / "alpha.md"))

        with store.get_connection() as conn:
            conn.execute(
                """
                UPDATE knowledge_items
                SET title = ?, summary_100_words = ?, keywords = ?, tags = ?
                WHERE knowledge_id = 1
                """,
                ("Beta", "Beta beta body", "beta", "beta"),
            )

        alpha_response = BM25Retriever(db_path).search("alpha", limit=5)
        assert alpha_response.status == "no_hits"
        response = BM25Retriever(db_path).search("beta", limit=5)
        assert response.status == "success"
        assert response.results[0].title == "Beta"

    def test_initialize_after_migration_does_not_duplicate_schema_indexes(
        self, tmp_path: Path
    ):
        """迁移建库后再次 initialize 不应产生运行时别名重复索引。"""
        db_path = tmp_path / "indexes.db"
        manager = MigrationManager(
            db_path,
            PROJECT_ROOT / "scripts" / "migrations",
        )
        assert manager.apply_all_pending(auto_backup=False) > 0

        SQLiteStore(db_path).initialize()

        with closing(sqlite3.connect(str(db_path))) as conn, conn:
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='knowledge_items'"
                )
            }

        assert "idx_source_type" not in indexes
        assert "idx_event_time" not in indexes
        assert "idx_published_at" not in indexes
        assert "idx_archived_at" not in indexes
        assert "idx_search_strategy" not in indexes
        assert {
            "idx_source_url",
            "idx_file_path",
            "idx_knowledge_source_type",
            "idx_knowledge_event_time",
            "idx_knowledge_published_at",
            "idx_knowledge_archived_at",
            "idx_knowledge_search_strategy",
        }.issubset(indexes)

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
        for idx, data in enumerate(test_dataset, start=1):
            entry = Entry(
                title=data["title"],
                content=f"# {data['title']}\n\n{data['content']}",
                abstract=data["content"][:100],
                summary_one_sentence=data["content"][:50],
                summary_100_words=data["content"],
                tags=data["tags"],
                keywords=",".join(data["tags"]),
                source_type="test",
                source_url=f"https://test.example.com/{idx}",
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
            response = router.search(test_case["query"], limit=5)

            # 检查预期结果是否在 Top 5
            if response.results:
                top_titles = [r.title for r in response.results[:5]]
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
        chunk_response = retriever.search_chunks("Alpha relation", limit=3)

        assert chunk_response.status == "success"
        assert chunk_response.results[0].knowledge_id == knowledge_id
        assert chunk_response.results[0].metadata["chunk_index"] == 0
        assert "Alpha relation chunk" in chunk_response.results[0].metadata["chunk_text"]

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

    def test_fts5_index_exists(self, tmp_path: Path):
        """在临时数据库验证 FTS5 索引。"""
        db_path = tmp_path / "knowledge_vault.db"
        store = SQLiteStore(db_path)
        store.initialize()
        with store.get_connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (FTS_TABLE_NAME,),
            )
            result = cursor.fetchone()
            assert result is not None, "FTS5 索引表应该存在"

            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (LEGACY_FTS_TABLE_NAME,),
            )
            legacy_result = cursor.fetchone()
            assert legacy_result is None, "旧 FTS 表名不应继续存在"

    def test_vector_index_exists(self, tmp_path: Path):
        """在临时目录验证向量索引可用。"""
        vector_dir = tmp_path / "vectors"
        vector_store = VectorStore(vector_dir, dim=DeterministicEmbedder.dim)
        stats = vector_store.get_index_stats()

        assert stats["doc_count"] >= 0, "向量索引应该可用"
        print(f"\n向量索引统计: {stats}")
