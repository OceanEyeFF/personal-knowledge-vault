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

FTS_TABLE_NAME = "knowledge_items_fts"
LEGACY_FTS_TABLE_NAME = "knowledge_fts"
LEGACY_FTS_TRIGGER_NAMES = (
    "knowledge_fts_insert",
    "knowledge_fts_update",
    "knowledge_fts_delete",
)


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
            self._ensure_timeline_time_columns(conn)

            # 2. 创建索引
            self._create_indexes(conn)

            # 3. 创建 FTS5 虚拟表和触发器
            rebuild_fts = self._ensure_fts5_contract(conn)
            if rebuild_fts:
                self._rebuild_fts5_index(conn)

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
                event_time TIMESTAMP,
                published_at TIMESTAMP,
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

    def _ensure_timeline_time_columns(self, conn: sqlite3.Connection) -> None:
        """为旧库补齐 timeline 相关真实时间字段。"""
        cursor = conn.execute("PRAGMA table_info(knowledge_items)")
        columns = {row["name"] for row in cursor.fetchall()}

        if "event_time" not in columns:
            conn.execute("ALTER TABLE knowledge_items ADD COLUMN event_time TIMESTAMP")
        if "published_at" not in columns:
            conn.execute("ALTER TABLE knowledge_items ADD COLUMN published_at TIMESTAMP")

    def _create_indexes(self, conn: sqlite3.Connection):
        """创建所有索引"""
        logger.info("创建索引...")

        indexes = [
            # knowledge_items 索引
            "CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url)",
            "CREATE INDEX IF NOT EXISTS idx_source_type ON knowledge_items(source_type)",
            "CREATE INDEX IF NOT EXISTS idx_event_time ON knowledge_items(event_time)",
            "CREATE INDEX IF NOT EXISTS idx_published_at ON knowledge_items(published_at)",
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

    def _ensure_fts5_contract(self, conn: sqlite3.Connection) -> bool:
        """确保运行时与迁移链使用同一套 FTS 合同。"""
        legacy_exists = self._sqlite_object_exists(conn, "table", LEGACY_FTS_TABLE_NAME)
        current_exists = self._sqlite_object_exists(conn, "table", FTS_TABLE_NAME)

        self._drop_legacy_fts_contract(conn)
        self._create_fts5_table(conn)
        return legacy_exists or not current_exists

    def _create_fts5_table(self, conn: sqlite3.Connection):
        """创建 FTS5 全文搜索虚拟表和触发器"""
        logger.info("创建 FTS5 全文搜索虚拟表...")

        # 创建 FTS5 虚拟表
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE_NAME} USING fts5(
                title,
                summary_100_words,
                keywords,
                tags,
                content=knowledge_items,
                content_rowid=knowledge_id
            )
        """)

        # 创建触发器: 插入
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_ai AFTER INSERT ON knowledge_items BEGIN
                INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
            END
        """)

        # 创建触发器: 删除
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_ad AFTER DELETE ON knowledge_items BEGIN
                DELETE FROM {FTS_TABLE_NAME} WHERE rowid = old.knowledge_id;
            END
        """)

        # 创建触发器: 更新
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS knowledge_items_au AFTER UPDATE ON knowledge_items BEGIN
                DELETE FROM {FTS_TABLE_NAME} WHERE rowid = old.knowledge_id;
                INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
                VALUES (new.knowledge_id, new.title, new.summary_100_words, new.keywords, new.tags);
            END
        """)

        logger.info("✓ FTS5 虚拟表和触发器创建成功")

    def _drop_legacy_fts_contract(self, conn: sqlite3.Connection) -> None:
        """清理旧版 knowledge_fts 合同。"""
        for trigger_name in LEGACY_FTS_TRIGGER_NAMES:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.execute(f"DROP TABLE IF EXISTS {LEGACY_FTS_TABLE_NAME}")

    def _rebuild_fts5_index(self, conn: sqlite3.Connection) -> None:
        """为已有条目重建 FTS5 索引。"""
        cursor = conn.execute(
            """
            SELECT knowledge_id, title, summary_100_words, keywords, tags
            FROM knowledge_items
            ORDER BY knowledge_id ASC
            """
        )
        rows = cursor.fetchall()

        conn.execute(f"DELETE FROM {FTS_TABLE_NAME}")
        if not rows:
            return

        fts_rows = []
        for row in rows:
            fts5_data = self.text_processor.prepare_fts5_data(
                row["title"] or "",
                row["summary_100_words"] or "",
                row["keywords"] or "",
                row["tags"] or "",
            )
            fts_rows.append(
                (
                    row["knowledge_id"],
                    fts5_data["title"],
                    fts5_data["summary_100_words"],
                    fts5_data["keywords"],
                    fts5_data["tags"],
                )
            )

        conn.executemany(
            f"""
            INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
            VALUES (?, ?, ?, ?, ?)
            """,
            fts_rows,
        )

    @staticmethod
    def _sqlite_object_exists(
        conn: sqlite3.Connection, object_type: str, name: str
    ) -> bool:
        cursor = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = ? AND name = ?
            """,
            (object_type, name),
        )
        return cursor.fetchone() is not None

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
                    file_path, word_count, event_time, published_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                entry.event_time,
                entry.published_at,
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
            conn.execute(f"DELETE FROM {FTS_TABLE_NAME} WHERE rowid = ?", (knowledge_id,))

            # 插入分词后的数据
            conn.execute(f"""
                INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
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

    def insert_chunks(self, knowledge_id: int, chunks: List[str]) -> int:
        """为知识条目插入分块文本。

        Args:
            knowledge_id: 知识条目 ID
            chunks: 分块文本列表

        Returns:
            实际插入的分块数量
        """
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if not chunks:
            return 0

        chunk_rows = []
        for chunk_index, chunk_text in enumerate(chunks):
            chunk_text_clean = (chunk_text or "").strip()
            if not chunk_text_clean:
                continue
            chunk_rows.append((knowledge_id, chunk_index, chunk_text_clean))

        if not chunk_rows:
            return 0

        with self.get_connection() as conn:
            conn.executemany(
                """
                INSERT INTO content_chunks (
                    knowledge_id, chunk_index, chunk_text
                ) VALUES (?, ?, ?)
                ON CONFLICT(knowledge_id, chunk_index) DO UPDATE SET
                    chunk_text = excluded.chunk_text
                """,
                chunk_rows,
            )

        logger.info(
            f"插入内容分块: knowledge_id={knowledge_id}, count={len(chunk_rows)}"
        )
        return len(chunk_rows)

    def get_chunks_by_knowledge_id(self, knowledge_id: int) -> List[Dict[str, Any]]:
        """获取条目对应的全部分块。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT chunk_id, knowledge_id, chunk_index, chunk_text,
                       context_before, context_after, section_title, created_at
                FROM content_chunks
                WHERE knowledge_id = ?
                ORDER BY chunk_index ASC
                """,
                (knowledge_id,),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]

    def get_chunk_by_index(
        self, knowledge_id: int, chunk_index: int
    ) -> Optional[Dict[str, Any]]:
        """按 knowledge_id 与 chunk_index 查询单个分块。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")
        if chunk_index < 0:
            raise ValueError("chunk_index 不能为负数")

        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT chunk_id, knowledge_id, chunk_index, chunk_text,
                       context_before, context_after, section_title, created_at
                FROM content_chunks
                WHERE knowledge_id = ? AND chunk_index = ?
                """,
                (knowledge_id, chunk_index),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def delete_chunks_by_knowledge_id(self, knowledge_id: int) -> int:
        """删除条目对应的全部分块。"""
        if knowledge_id <= 0:
            raise ValueError("knowledge_id 必须为正整数")

        with self.get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM content_chunks WHERE knowledge_id = ?",
                (knowledge_id,),
            )
            deleted_count = cursor.rowcount

        logger.info(f"删除内容分块: knowledge_id={knowledge_id}, count={deleted_count}")
        return deleted_count

    def count_chunks(self, knowledge_id: Optional[int] = None) -> int:
        """统计分块数量。"""
        with self.get_connection() as conn:
            if knowledge_id is None:
                cursor = conn.execute("SELECT COUNT(*) AS cnt FROM content_chunks")
            else:
                if knowledge_id <= 0:
                    raise ValueError("knowledge_id 必须为正整数")
                cursor = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM content_chunks WHERE knowledge_id = ?",
                    (knowledge_id,),
                )
            row = cursor.fetchone()
            return int(row["cnt"]) if row else 0

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

    def delete_entry(self, knowledge_id: int) -> bool:
        """删除知识条目及所有关联数据。

        级联删除 content_chunks、knowledge_tags、video_timestamps（外键 CASCADE）。
        FTS5 触发器自动清理全文索引。
        删除后递减相关标签计数，计数归零的标签自动清理。

        Args:
            knowledge_id: 知识条目 ID。

        Returns:
            True 表示成功删除，False 表示条目不存在。
        """
        with self.get_connection() as conn:
            # 1. 先递减标签计数（必须在 CASCADE 删除 knowledge_tags 之前）
            self._decrement_tag_counts(conn, knowledge_id)

            # 2. 删除主表记录（CASCADE 自动清理 chunks/tags关联/timestamps，触发器清理 FTS5）
            cursor = conn.execute(
                "DELETE FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,),
            )

            deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"删除知识条目: knowledge_id={knowledge_id}")
            else:
                logger.warning(f"条目不存在: knowledge_id={knowledge_id}")
            return deleted

    def _decrement_tag_counts(self, conn: sqlite3.Connection, knowledge_id: int) -> None:
        """递减条目关联标签的计数，计数归零时删除标签。

        Args:
            conn: 数据库连接（在同一事务中调用）。
            knowledge_id: 即将被删除的条目 ID。
        """
        # 查询该条目关联的所有标签 ID
        cursor = conn.execute(
            "SELECT tag_id FROM knowledge_tags WHERE knowledge_id = ?",
            (knowledge_id,),
        )
        tag_ids = [row[0] for row in cursor.fetchall()]

        for tag_id in tag_ids:
            conn.execute("UPDATE tags SET count = count - 1 WHERE tag_id = ?", (tag_id,))

        # 清理计数归零的标签
        if tag_ids:
            conn.execute("DELETE FROM tags WHERE count <= 0")

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

    # ==========================================
    # AI 对话会话管理方法 (M12)
    # ==========================================

    def create_session(self, session_id: str, title: str) -> None:
        """
        创建新的 AI 对话会话

        Args:
            session_id: 会话 ID（UUID 格式）
            title: 会话标题

        Raises:
            Exception: 创建失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO chat_sessions (session_id, title, messages)
                    VALUES (?, ?, '[]')
                    """,
                    (session_id, title)
                )
            logger.info(f"✅ 创建会话: {session_id} - {title}")
        except Exception as e:
            logger.error(f"创建会话失败: {e}")
            raise

    def update_session(
        self,
        session_id: str,
        messages: List[dict],
        total_tokens: int,
        round_count: int,
        summary: Optional[str] = None
    ) -> None:
        """
        更新会话内容

        Args:
            session_id: 会话 ID
            messages: 消息列表（OpenAI 格式）
            total_tokens: 累计 token 数
            round_count: 对话轮数
            summary: AI 生成的摘要（可选）

        Raises:
            Exception: 更新失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET messages = ?,
                        total_tokens = ?,
                        round_count = ?,
                        summary = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                    """,
                    (
                        json.dumps(messages, ensure_ascii=False),
                        total_tokens,
                        round_count,
                        summary,
                        session_id
                    )
                )
            logger.info(f"✅ 更新会话: {session_id} (轮数: {round_count}, Tokens: {total_tokens})")
        except Exception as e:
            logger.error(f"更新会话失败: {e}")
            raise

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        获取会话详情

        Args:
            session_id: 会话 ID

        Returns:
            会话字典，包含所有字段（messages 已解析为 list）
            如果会话不存在则返回 None

        Raises:
            Exception: 查询失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT session_id, title, created_at, updated_at,
                           messages, summary, total_tokens, round_count,
                           is_archived, knowledge_id
                    FROM chat_sessions
                    WHERE session_id = ?
                    """,
                    (session_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return None

                session_dict = dict(row)
                # 解析 JSON messages
                session_dict["messages"] = json.loads(session_dict["messages"])
                return session_dict
        except Exception as e:
            logger.error(f"获取会话失败: {e}")
            raise

    def list_sessions(
        self,
        is_archived: bool = False,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        列出会话列表

        Args:
            is_archived: 是否只显示归档会话
            limit: 最大返回数量

        Returns:
            会话列表（按更新时间倒序）

        Raises:
            Exception: 查询失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT session_id, title, created_at, updated_at,
                           total_tokens, round_count, is_archived,
                           knowledge_id, summary
                    FROM chat_sessions
                    WHERE is_archived = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (1 if is_archived else 0, limit)
                )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"列出会话失败: {e}")
            raise

    def delete_session(self, session_id: str) -> None:
        """
        删除会话

        Args:
            session_id: 会话 ID

        Raises:
            Exception: 删除失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                conn.execute(
                    "DELETE FROM chat_sessions WHERE session_id = ?",
                    (session_id,)
                )
            logger.info(f"✅ 删除会话: {session_id}")
        except Exception as e:
            logger.error(f"删除会话失败: {e}")
            raise

    def archive_session(self, session_id: str, is_archived: bool = True) -> None:
        """
        归档或取消归档会话

        Args:
            session_id: 会话 ID
            is_archived: True=归档, False=取消归档

        Raises:
            Exception: 操作失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET is_archived = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                    """,
                    (1 if is_archived else 0, session_id)
                )
            action = "归档" if is_archived else "取消归档"
            logger.info(f"✅ {action}会话: {session_id}")
        except Exception as e:
            logger.error(f"归档会话失败: {e}")
            raise

    def link_session_to_knowledge(
        self,
        session_id: str,
        knowledge_id: int
    ) -> None:
        """
        关联会话到知识条目

        Args:
            session_id: 会话 ID
            knowledge_id: 知识条目 ID

        Raises:
            Exception: 关联失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                conn.execute(
                    """
                    UPDATE chat_sessions
                    SET knowledge_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                    """,
                    (knowledge_id, session_id)
                )
            logger.info(f"✅ 关联会话 {session_id} -> 知识条目 {knowledge_id}")
        except Exception as e:
            logger.error(f"关联会话失败: {e}")
            raise

    def get_session_stats(self) -> Dict[str, Any]:
        """
        获取会话统计信息

        Returns:
            统计数据字典，包含：
            - total_sessions: 总会话数
            - active_sessions: 活跃会话数
            - archived_sessions: 归档会话数
            - total_tokens: 累计 token 消耗
            - avg_tokens_per_session: 平均每会话 token 数
            - total_rounds: 累计对话轮数

        Raises:
            Exception: 查询失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT
                        COUNT(*) as total_sessions,
                        SUM(CASE WHEN is_archived = 0 THEN 1 ELSE 0 END) as active_sessions,
                        SUM(CASE WHEN is_archived = 1 THEN 1 ELSE 0 END) as archived_sessions,
                        COALESCE(SUM(total_tokens), 0) as total_tokens,
                        COALESCE(AVG(total_tokens), 0) as avg_tokens_per_session,
                        COALESCE(SUM(round_count), 0) as total_rounds
                    FROM chat_sessions
                    """
                )
                row = cursor.fetchone()
                return dict(row) if row else {}
        except Exception as e:
            logger.error(f"获取会话统计失败: {e}")
            raise

    def get_all_sessions_stats(self) -> List[Dict[str, Any]]:
        """
        获取所有会话的统计概览

        Returns:
            会话概览列表，每个会话包含：
            - session_id
            - title
            - created_at
            - updated_at
            - total_tokens
            - round_count
            - is_archived

        Raises:
            Exception: 查询失败时抛出异常
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT session_id, title, created_at, updated_at,
                           total_tokens, round_count, is_archived
                    FROM chat_sessions
                    ORDER BY updated_at DESC
                    """
                )
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"获取所有会话统计失败: {e}")
            raise
