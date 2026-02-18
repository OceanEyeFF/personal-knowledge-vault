"""
SQLite 存储层

负责 SQLite 数据库的初始化、索引管理和 CRUD 操作
"""

import sqlite3
import json
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager

from src.utils.logger import get_logger
from src.utils.text_utils import TextProcessor
from src.storage.markdown_store import Entry

logger = get_logger(__name__)


class SQLiteStore:
    """SQLite 数据库存储管理器"""

    ALLOWED_SORT_FIELDS = {"archived_at", "title", "knowledge_id", "word_count", "source_type"}
    ALLOWED_SORT_ORDERS = {"asc", "desc"}

    def __init__(self, db_path: Path):
        """
        初始化 SQLite 存储

        Args:
            db_path: 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self.text_processor = TextProcessor()  # 用于 FTS5 分词
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
                knowledge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                summary_one_sentence TEXT,
                summary_100_words TEXT,
                keywords TEXT,
                tags TEXT,
                outline TEXT,
                source_type TEXT NOT NULL CHECK(source_type IN ('wechat', 'zhihu', 'bilibili', 'webpage', 'article', 'document', 'generic', 'personal', 'ai_chat', 'text', 'test')),
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
            )
        """)

        # tags (标签表)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                tag_id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(tag_id) ON DELETE CASCADE
            )
        """)

        # video_timestamps (视频时间轴表, Phase 2)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS video_timestamps (
                timestamp_id INTEGER PRIMARY KEY AUTOINCREMENT,
                knowledge_id INTEGER NOT NULL,
                timestamp_seconds INTEGER NOT NULL,
                segment_text TEXT NOT NULL,
                chapter_title TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge_items(knowledge_id) ON DELETE CASCADE,
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
                content_rowid=knowledge_id
            )
        """)

        # 创建触发器: 插入
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
                INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
            END
        """)

        # 创建触发器: 删除
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
                DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
            END
        """)

        # 创建触发器: 更新
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
                DELETE FROM knowledge_items_fts WHERE rowid = old.knowledge_id;
                INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
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
            # 插入主表（使用原始数据，不分词）
            # FTS5 虚拟表会通过触发器自动同步并分词
            cursor = conn.execute("""
                INSERT INTO knowledge_items (
                    title, content, summary_one_sentence, summary_100_words,
                    keywords, tags, source_type, source_url, search_strategy,
                    file_path, word_count, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.title,  # 使用原始标题（修复：不再分词）
                entry.content,
                entry.summary_one_sentence,
                entry.summary_100_words,  # 使用原始摘要（修复：不再分词）
                ",".join(entry.keywords) if isinstance(entry.keywords, list) else entry.keywords,  # 转换列表为字符串
                ",".join(entry.tags) if isinstance(entry.tags, list) else entry.tags,  # 转换列表为字符串
                entry.source_type,
                entry.source_url,
                entry.search_strategy,
                file_path,
                entry.word_count,
                entry.archived_at
            ))

            knowledge_id = cursor.lastrowid

            # 手动更新 FTS5 表（使用分词后的数据）
            # 注意：触发器会自动插入原始数据，我们需要用分词后的数据覆盖
            fts5_data = self.text_processor.prepare_fts5_data(
                entry.title,
                entry.summary_100_words or "",
                entry.keywords or "",
                ",".join(entry.tags) if isinstance(entry.tags, list) else (entry.tags or "")
            )

            # 删除触发器自动插入的原始数据
            conn.execute("DELETE FROM knowledge_items_fts WHERE rowid = ?", (knowledge_id,))

            # 插入分词后的数据
            conn.execute("""
                INSERT INTO knowledge_items_fts(rowid, title, summary_100_words, keywords, tags)
                VALUES (?, ?, ?, ?, ?)
            """, (
                knowledge_id,
                fts5_data["title"],
                fts5_data["summary_100_words"],
                fts5_data["keywords"],
                fts5_data["tags"]
            ))

            # 插入标签关联
            self._insert_tags(conn, knowledge_id, entry.tags)

            logger.info(f"插入知识条目: ID={knowledge_id}, title={entry.title}")
            return knowledge_id

    def _insert_tags(self, conn: sqlite3.Connection, knowledge_id: int, tags: List[str]):
        """插入标签关联"""
        for tag_name in tags:
            # 获取或创建标签
            cursor = conn.execute("SELECT tag_id FROM tags WHERE name = ?", (tag_name,))
            row = cursor.fetchone()

            if row:
                tag_id = row[0]
                # 更新计数
                conn.execute("UPDATE tags SET count = count + 1 WHERE tag_id = ?", (tag_id,))
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
            cursor = conn.execute("SELECT * FROM knowledge_items WHERE knowledge_id = ?", (knowledge_id,))
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

    def query_by_url(self, source_url: str) -> Optional[Dict[str, Any]]:
        """
        根据来源 URL 查询知识条目

        Args:
            source_url: 来源 URL

        Returns:
            字典形式的条目数据
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM knowledge_items WHERE source_url = ?",
                    (source_url,)
                )
                row = cursor.fetchone()
                if row:
                    return dict(row)
                return None
        except Exception as e:
            logger.error(f"根据来源 URL 查询失败: {e}")
            raise

    def list_entries(
        self,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "archived_at",
        sort_order: str = "desc",
        source_type: Optional[str] = None,
        tag: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        获取知识条目列表

        Args:
            limit: 返回数量
            offset: 偏移量
            sort_by: 排序字段
            sort_order: 排序顺序
            source_type: 来源类型过滤
            tag: 标签过滤

        Returns:
            知识条目列表
        """
        if sort_by not in self.ALLOWED_SORT_FIELDS:
            raise ValueError(f"无效的排序字段: {sort_by}")
        sort_order_lower = sort_order.lower()
        if sort_order_lower not in self.ALLOWED_SORT_ORDERS:
            raise ValueError(f"无效的排序顺序: {sort_order}")

        try:
            query = "SELECT ki.* FROM knowledge_items ki"
            params: List[Any] = []
            conditions: List[str] = []

            if tag:
                query += " JOIN knowledge_tags kt ON ki.knowledge_id = kt.knowledge_id"
                query += " JOIN tags t ON kt.tag_id = t.tag_id"
                conditions.append("t.name = ?")
                params.append(tag)

            if source_type:
                conditions.append("ki.source_type = ?")
                params.append(source_type)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += f" ORDER BY ki.{sort_by} {sort_order_lower} LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            with self.get_connection() as conn:
                cursor = conn.execute(query, tuple(params))
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"获取知识条目列表失败: {e}")
            raise

    def count_entries(self, source_type: Optional[str] = None, tag: Optional[str] = None) -> int:
        """
        获取知识条目数量

        Args:
            source_type: 来源类型过滤
            tag: 标签过滤

        Returns:
            条目数量
        """
        try:
            params: List[Any] = []
            conditions: List[str] = []

            if tag:
                query = "SELECT COUNT(DISTINCT ki.knowledge_id) AS cnt FROM knowledge_items ki"
                query += " JOIN knowledge_tags kt ON ki.knowledge_id = kt.knowledge_id"
                query += " JOIN tags t ON kt.tag_id = t.tag_id"
                conditions.append("t.name = ?")
                params.append(tag)
            else:
                query = "SELECT COUNT(*) AS cnt FROM knowledge_items ki"

            if source_type:
                conditions.append("ki.source_type = ?")
                params.append(source_type)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            with self.get_connection() as conn:
                cursor = conn.execute(query, tuple(params))
                row = cursor.fetchone()
                return int(row["cnt"]) if row else 0
        except Exception as e:
            logger.error(f"获取知识条目数量失败: {e}")
            raise

    def count_entries_by_source_type(self) -> List[Tuple[str, int]]:
        """
        按来源类型统计条目数量

        Returns:
            (来源类型, 数量) 列表
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT source_type, COUNT(*) as cnt FROM knowledge_items GROUP BY source_type"
                )
                rows = cursor.fetchall()
                return [(row["source_type"], row["cnt"]) for row in rows]
        except Exception as e:
            logger.error(f"按来源类型统计失败: {e}")
            raise

    def get_all_tags_with_count(self, limit: int = 0) -> List[Dict[str, Any]]:
        """
        获取全部标签及其计数

        Args:
            limit: 限制返回数量，0 表示不限制

        Returns:
            标签列表
        """
        try:
            query = "SELECT name, count FROM tags ORDER BY count DESC"
            params: List[Any] = []
            if limit > 0:
                query += " LIMIT ?"
                params.append(limit)

            with self.get_connection() as conn:
                cursor = conn.execute(query, tuple(params))
                rows = cursor.fetchall()
                return [{"name": row["name"], "count": row["count"]} for row in rows]
        except Exception as e:
            logger.error(f"获取标签计数失败: {e}")
            raise

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取统计信息

        Returns:
            统计数据字典
        """
        try:
            return {
                "total_entries": self.count_entries(),
                "by_source_type": self.count_entries_by_source_type(),
                "top_tags": self.get_all_tags_with_count(limit=20)
            }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            raise
