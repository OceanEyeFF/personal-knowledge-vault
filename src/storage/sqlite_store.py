"""
SQLite 存储层

负责 SQLite 数据库的初始化、索引管理和 CRUD 操作
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from src.utils.logger import get_logger
from src.utils.text_utils import TextProcessor
from src.storage.markdown_store import Entry

logger = get_logger(__name__)


class SQLiteStore:
    """SQLite 数据库存储管理器"""

    def __init__(self, db_path: Path):
        """
        初始化 SQLite 存储

        Args:
            db_path: 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        logger.info(f"SQLite 存储初始化: {self.db_path}")

    @contextmanager
    def get_connection(self):
        """
        获取数据库连接 (上下文管理器)

        Yields:
            sqlite3.Connection
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 使用字典模式访问列
        conn.execute("PRAGMA foreign_keys = ON")  # 启用外键约束
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库操作失败: {e}")
            raise
        finally:
            conn.close()

    def initialize(self):
        """初始化数据库 Schema"""
        logger.info("开始初始化数据库...")

        with self.get_connection() as conn:
            # 1. 创建主表
            self._create_tables(conn)

            # 2. 创建索引
            self._create_indexes(conn)

            # 3. 创建 FTS5 虚拟表和触发器
            self._create_fts5_table(conn)

            # 4. 验证完整性
            self._verify_integrity(conn)

        logger.info("✅ 数据库初始化完成！")

    def _create_tables(self, conn: sqlite3.Connection):
        """创建所有表"""
        logger.info("创建数据表...")

        # knowledge_items (主知识表)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                summary_one_sentence TEXT,
                summary_100_words TEXT,
                keywords TEXT,
                tags TEXT,
                outline TEXT,
                source_type TEXT NOT NULL CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'generic', 'personal')),
                source_url TEXT UNIQUE,
                search_strategy TEXT CHECK(search_strategy IN ('keyword', 'hybrid', 'vector', 'structured')),
                file_path TEXT NOT NULL UNIQUE,
                word_count INTEGER DEFAULT 0,
                archived_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # content_chunks (长文本分块表)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT NOT NULL,
                context_before TEXT,
                context_after TEXT,
                section_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(id) ON DELETE CASCADE,
                UNIQUE(knowledge_id, chunk_index)
            )
        """)

        # tags (标签表)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                tag_group TEXT,
                count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # knowledge_tags (知识-标签关联表)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_tags (
                knowledge_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (knowledge_id, tag_id),
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        """)

        # video_timestamps (视频时间轴表, Phase 2)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_timestamps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                timestamp_seconds INTEGER NOT NULL,
                segment_text TEXT NOT NULL,
                chapter_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(id) ON DELETE CASCADE,
                UNIQUE(knowledge_id, timestamp_seconds)
            )
        """)

        logger.info("✓ 数据表创建成功")

    def _create_indexes(self, conn: sqlite3.Connection):
        """创建所有索引"""
        logger.info("创建索引...")

        indexes = [
            # knowledge_items 索引
            "CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url)",
            "CREATE INDEX IF NOT EXISTS idx_source_type ON knowledge_items(source_type)",
            "CREATE INDEX IF NOT EXISTS idx_archived_at ON knowledge_items(archived_at)",
            "CREATE INDEX IF NOT EXISTS idx_search_strategy ON knowledge_items(search_strategy)",
            "CREATE INDEX IF NOT EXISTS idx_file_path ON knowledge_items(file_path)",
            # content_chunks 索引
            "CREATE INDEX IF NOT EXISTS idx_knowledge_chunk ON content_chunks(knowledge_id, chunk_index)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_id ON content_chunks(knowledge_id)",
            # tags 索引
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name)",
            "CREATE INDEX IF NOT EXISTS idx_tag_group ON tags(tag_group)",
            # knowledge_tags 索引
            "CREATE INDEX IF NOT EXISTS idx_kt_knowledge_id ON knowledge_tags(knowledge_id)",
            "CREATE INDEX IF NOT EXISTS idx_kt_tag_id ON knowledge_tags(tag_id)",
            # video_timestamps 索引
            "CREATE INDEX IF NOT EXISTS idx_knowledge_timestamp ON video_timestamps(knowledge_id, timestamp_seconds)",
            "CREATE INDEX IF NOT EXISTS idx_vt_knowledge_id ON video_timestamps(knowledge_id)",
        ]

        for sql in indexes:
            conn.execute(sql)

        logger.info("✓ 索引创建成功")

    def _create_fts5_table(self, conn: sqlite3.Connection):
        """创建 FTS5 全文搜索虚拟表和触发器"""
        logger.info("创建 FTS5 全文搜索虚拟表...")

        # 创建 FTS5 虚拟表
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_items_fts USING fts5(
                title,
                summary_100_words,
                keywords,
                tags,
                content=knowledge_items,
                content_rowid=id
            )
        """)

        # 创建触发器: 插入
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
                INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.id, new.title, new.summary_100_words, new.keywords, new.tags);
            END
        """)

        # 创建触发器: 删除
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
                DELETE FROM knowledge_items_fts WHERE rowid = old.id;
            END
        """)

        # 创建触发器: 更新
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
                DELETE FROM knowledge_items_fts WHERE rowid = old.id;
                INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.id, new.title, new.summary_100_words, new.keywords, new.tags);
            END
        """)

        logger.info("✓ FTS5 虚拟表和触发器创建成功")

    def _verify_integrity(self, conn: sqlite3.Connection):
        """验证数据库完整性"""
        logger.info("验证数据库完整性...")

        # 检查外键约束
        cursor = conn.execute("PRAGMA foreign_key_check")
        fk_violations = cursor.fetchall()
        if fk_violations:
            logger.warning(f"⚠️  发现外键约束违规: {fk_violations}")
        else:
            logger.info("✓ 外键约束检查通过")

        # 检查表完整性
        cursor = conn.execute("PRAGMA integrity_check")
        integrity_result = cursor.fetchone()[0]
        if integrity_result == "ok":
            logger.info("✓ 数据库完整性检查通过")
        else:
            logger.warning(f"⚠️  数据库完整性问题: {integrity_result}")

    def insert_entry(self, entry: Entry, file_path: str) -> int:
        """
        插入知识条目

        Args:
            entry: 知识条目
            file_path: Markdown 文件路径

        Returns:
            插入的条目 ID
        """
        with self.get_connection() as conn:
            # 准备 FTS5 数据 (jieba 分词)
            text_processor = TextProcessor()
            fts5_data = text_processor.prepare_fts5_data(
                entry.title,
                entry.summary_100_words,
                entry.keywords,
                entry.tags
            )

            # 插入主表
            cursor = conn.execute("""
                INSERT INTO knowledge_items (
                    title, content, summary_one_sentence, summary_100_words,
                    keywords, tags, source_type, source_url, search_strategy,
                    file_path, word_count, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fts5_data["title"],  # 使用分词后的标题
                entry.content,
                entry.summary_one_sentence,
                fts5_data["summary_100_words"],
                fts5_data["keywords"],
                fts5_data["tags"],
                entry.source_type,
                entry.source_url,
                entry.search_strategy,
                file_path,
                entry.word_count,
                entry.archived_at
            ))

            knowledge_id = cursor.lastrowid

            # 插入标签关联
            self._insert_tags(conn, knowledge_id, entry.tags)

            logger.info(f"插入知识条目: ID={knowledge_id}, title={entry.title}")
            return knowledge_id

    def _insert_tags(self, conn: sqlite3.Connection, knowledge_id: int, tags: List[str]):
        """插入标签关联"""
        for tag_name in tags:
            # 获取或创建标签
            cursor = conn.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
            row = cursor.fetchone()

            if row:
                tag_id = row[0]
                # 更新计数
                conn.execute("UPDATE tags SET count = count + 1 WHERE id = ?", (tag_id,))
            else:
                # 创建新标签
                cursor = conn.execute("INSERT INTO tags (name, count) VALUES (?, 1)", (tag_name,))
                tag_id = cursor.lastrowid

            # 插入关联
            conn.execute(
                "INSERT OR IGNORE INTO knowledge_tags (knowledge_id, tag_id) VALUES (?, ?)",
                (knowledge_id, tag_id)
            )

    def query_by_id(self, knowledge_id: int) -> Optional[Dict[str, Any]]:
        """
        根据 ID 查询知识条目

        Args:
            knowledge_id: 知识条目 ID

        Returns:
            字典形式的条目数据
        """
        with self.get_connection() as conn:
            cursor = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (knowledge_id,))
            row = cursor.fetchone()

            if row:
                return dict(row)
            return None

    def table_exists(self, table_name: str) -> bool:
        """
        检查表是否存在

        Args:
            table_name: 表名

        Returns:
            是否存在
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            )
            return cursor.fetchone() is not None
