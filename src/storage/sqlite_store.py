"""
SQLite 存储层

负责 SQLite 数据库的初始化、索引管理和 CRUD 操作
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.storage.markdown_store import Entry
from src.storage.sqlite_connection import connect_existing_sqlite
from src.utils.logger import get_logger
from src.utils.text_utils import TextProcessor

if TYPE_CHECKING:
    from src.runtime.layout import RuntimeLayout

logger = get_logger(__name__)

FTS_TABLE_NAME = "knowledge_items_fts"
LEGACY_FTS_TABLE_NAME = "knowledge_fts"
CURRENT_FTS_TRIGGER_NAMES = (
    "knowledge_items_ai",
    "knowledge_items_au",
    "knowledge_items_ad",
)
LEGACY_FTS_TRIGGER_NAMES = (
    "knowledge_fts_insert",
    "knowledge_fts_update",
    "knowledge_fts_delete",
)
OBSOLETE_INDEX_NAMES = (
    "idx_source_type",
    "idx_event_time",
    "idx_published_at",
    "idx_archived_at",
    "idx_search_strategy",
    "idx_knowledge_chunk",
    "idx_knowledge_id",
    "idx_knowledge_timestamp",
    "idx_vt_knowledge_id",
)

_CORE_PROJECTION_FIELDS = (
    "title",
    "content",
    "summary_one_sentence",
    "summary_100_words",
    "keywords",
    "tags",
    "source_type",
    "source_url",
    "search_strategy",
    "file_path",
    "word_count",
    "event_time",
    "published_at",
    "archived_at",
)


def _normalized_chunk_projection(chunks: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for chunk_index, chunk_text in enumerate(chunks):
        chunk_text_clean = (chunk_text or "").strip()
        if not chunk_text_clean:
            continue
        normalized.append(
            {"chunk_index": chunk_index, "chunk_text": chunk_text_clean}
        )
    return normalized


def _projection_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def entry_projection_sha256(
    entry: Entry,
    file_path: str,
    chunks: list[str],
) -> str:
    """Hash the exact SQLite-facing archive projection, excluding generated IDs."""

    keywords = ",".join(entry.keywords) if isinstance(entry.keywords, list) else entry.keywords
    tags = ",".join(entry.tags) if isinstance(entry.tags, list) else entry.tags
    values = {
        "title": entry.title,
        "content": entry.content,
        "summary_one_sentence": entry.summary_one_sentence,
        "summary_100_words": entry.summary_100_words,
        "keywords": keywords,
        "tags": tags,
        "source_type": entry.source_type,
        "source_url": entry.source_url,
        "search_strategy": entry.search_strategy,
        "file_path": file_path,
        "word_count": entry.word_count,
        "event_time": entry.event_time,
        "published_at": entry.published_at,
        "archived_at": entry.archived_at,
    }
    return _projection_digest(
        {
            "entry": {field: values[field] for field in _CORE_PROJECTION_FIELDS},
            "chunks": _normalized_chunk_projection(chunks),
        }
    )


def row_projection_sha256(
    row: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> str:
    """Hash an observed SQLite row/chunk projection using the same contract."""

    normalized_chunks = [
        {
            "chunk_index": int(chunk["chunk_index"]),
            "chunk_text": str(chunk["chunk_text"]),
        }
        for chunk in sorted(chunks, key=lambda item: int(item["chunk_index"]))
    ]
    return _projection_digest(
        {
            "entry": {field: row.get(field) for field in _CORE_PROJECTION_FIELDS},
            "chunks": normalized_chunks,
        }
    )


class SQLiteStore:
    """SQLite 数据库存储管理器"""

    ALLOWED_SORT_FIELDS = {"archived_at", "title", "knowledge_id", "word_count", "source_type"}
    ALLOWED_SORT_ORDERS = {"asc", "desc"}

    def __init__(
        self,
        db_path: Path,
        *,
        runtime_config: Any = None,
        text_processor: TextProcessor | None = None,
    ):
        """
        初始化 SQLite 存储

        Args:
            db_path: 数据库文件路径
            runtime_config: 所属 Application 的不可变 Config snapshot；产品路径
                显式传入，避免分词器回退全局 Config。
            text_processor: 已绑定同一 snapshot 的可复用分词器测试 seam。
        """
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self._runtime_config = runtime_config
        # Do not construct jieba during a SQLite read-only operation.  FTS
        # writes resolve this lazily; Application production factories inject
        # their captured Config so TextProcessor cannot fall back to global A.
        self._text_processor = text_processor
        logger.info("SQLite 存储初始化完成")

    @property
    def text_processor(self) -> TextProcessor:
        """Lazily resolve the FTS tokenizer only for a real FTS mutation."""

        if self._text_processor is None:
            self._text_processor = TextProcessor(runtime_config=self._runtime_config)
        return self._text_processor

    @contextmanager
    def get_connection(self):
        """
        获取数据库连接 (上下文管理器)

        Yields:
            sqlite3.Connection
        """
        conn = connect_existing_sqlite(self.db_path)
        try:
            # 初始化必须在 try 内: 任何 PRAGMA/配置失败时连接仍要关闭。
            conn.row_factory = sqlite3.Row  # 使用字典模式访问列
            conn.execute("PRAGMA foreign_keys = ON")  # 启用外键约束
            yield conn
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.error("数据库操作失败: error_type=%s", type(exc).__name__)
            raise
        finally:
            conn.close()

    def initialize(self, *, layout: RuntimeLayout | None = None):
        """Compatibility entrypoint routed through the migration authority.

        Two modes:

        * ``layout=...`` (preferred, production-shaped): the explicit
          :class:`RuntimeLayout` is the containment authority.  ``db_path`` is
          validated against the layout's user-data root and the layout's
          bundled migrations/backup directories are used.
        * no argument (tests/maintenance compatibility): the store's explicit
          ``db_path`` is honored as-is and no ambient user-data-root
          validation is applied; bundled migration scripts are still required
          and the schema is created exclusively through the migration chain.
          Product startup containment is enforced by ``bootstrap_runtime()``,
          not by this low-level wrapper.

        Existing databases are never repaired ad-hoc: drift, upgrade-required
        and future-version states keep their fail-closed rejections.
        """

        from src.runtime.layout import RuntimeLayout
        from src.storage.migration_manager import DatabaseState, MigrationManager

        if layout is None:
            # Compatibility mode: resolve only the bundled resources
            # (read-only); never validate the explicit db_path against an
            # ambient user-data root (that would reject tmp_path callers).
            resolved = RuntimeLayout.resolve()
            migrations_dir = resolved.migrations_dir
            backup_dir = None
        else:
            layout.validate_user_file(
                self.db_path,
                label="SQLite 数据库",
                allow_missing=True,
            )
            migrations_dir = layout.migrations_dir
            backup_dir = layout.backup_dir

        manager = MigrationManager(
            self.db_path,
            migrations_dir,
            backup_dir=backup_dir,
        )
        inspection = manager.require_ready()
        if inspection.state is DatabaseState.FRESH:
            manager.initialize_fresh()
        logger.info("✅ 数据库 migration contract 已就绪")

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
                source_type TEXT NOT NULL CHECK(
                    source_type IN (
                        'wechat', 'zhihu', 'bilibili', 'webpage', 'article',
                        'document', 'generic', 'personal', 'ai_chat', 'text', 'test'
                    )
                ),
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
        self._drop_obsolete_indexes(conn)

        indexes = [
            # knowledge_items 索引
            "CREATE INDEX IF NOT EXISTS idx_source_url ON knowledge_items(source_url)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_source_type ON knowledge_items(source_type)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_event_time ON knowledge_items(event_time)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_published_at ON knowledge_items(published_at)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_archived_at ON knowledge_items(archived_at)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_search_strategy ON knowledge_items(search_strategy)",
            "CREATE INDEX IF NOT EXISTS idx_file_path ON knowledge_items(file_path)",
            # content_chunks 索引
            "CREATE INDEX IF NOT EXISTS idx_chunks_index ON content_chunks(knowledge_id, chunk_index)",
            "CREATE INDEX IF NOT EXISTS idx_chunks_knowledge_id ON content_chunks(knowledge_id)",
            # tags 索引
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tags(name)",
            "CREATE INDEX IF NOT EXISTS idx_tag_group ON tags(tag_group)",
            # knowledge_tags 索引
            "CREATE INDEX IF NOT EXISTS idx_knowledge_tags_knowledge_id ON knowledge_tags(knowledge_id)",
            "CREATE INDEX IF NOT EXISTS idx_knowledge_tags_tag_id ON knowledge_tags(tag_id)",
            # video_timestamps 索引
            "CREATE INDEX IF NOT EXISTS idx_timestamps_time ON video_timestamps(knowledge_id, timestamp_seconds)",
            "CREATE INDEX IF NOT EXISTS idx_timestamps_knowledge_id ON video_timestamps(knowledge_id)",
        ]

        for sql in indexes:
            conn.execute(sql)

        logger.info("✓ 索引创建成功")

    def _ensure_fts5_contract(self, conn: sqlite3.Connection) -> bool:
        """确保运行时与迁移链使用同一套 FTS 合同。"""
        legacy_exists = self._sqlite_object_exists(conn, "table", LEGACY_FTS_TABLE_NAME)
        current_exists = self._sqlite_object_exists(conn, "table", FTS_TABLE_NAME)
        current_is_external_content = current_exists and self._fts_uses_external_content(
            conn, FTS_TABLE_NAME
        )

        self._drop_legacy_fts_contract(conn)
        if current_is_external_content:
            self._drop_current_fts_contract(conn)
        self._create_fts5_table(conn)
        return legacy_exists or not current_exists or current_is_external_content

    def _create_fts5_table(self, conn: sqlite3.Connection):
        """创建 FTS5 全文搜索虚拟表和触发器"""
        logger.info("创建 FTS5 全文搜索虚拟表...")

        # 创建 FTS5 虚拟表
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE_NAME} USING fts5(
                title,
                summary_100_words,
                keywords,
                tags
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

    def _drop_current_fts_contract(self, conn: sqlite3.Connection) -> None:
        """重建 knowledge_items_fts 前清理现有表与触发器。"""
        for trigger_name in CURRENT_FTS_TRIGGER_NAMES:
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE_NAME}")

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

    def rebuild_fts5_index(self) -> None:
        """公开的 FTS5 重建入口，供迁移链收敛到统一分词合同。"""
        with self.get_connection() as conn:
            self._create_fts5_table(conn)
            self._rebuild_fts5_index(conn)

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

    @staticmethod
    def _sqlite_object_sql(
        conn: sqlite3.Connection, object_type: str, name: str
    ) -> str:
        cursor = conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = ? AND name = ?
            """,
            (object_type, name),
        )
        row = cursor.fetchone()
        if row is None:
            return ""
        sql = row["sql"] if isinstance(row, sqlite3.Row) else row[0]
        return str(sql or "")

    @classmethod
    def _fts_uses_external_content(
        cls, conn: sqlite3.Connection, table_name: str
    ) -> bool:
        table_sql = cls._sqlite_object_sql(conn, "table", table_name).lower()
        normalized_sql = re.sub(r"\s+", "", table_sql)
        return bool(
            re.search(
                r"content=['\"]?knowledge_items['\"]?",
                normalized_sql,
            )
        )

    @staticmethod
    def _drop_obsolete_indexes(conn: sqlite3.Connection) -> None:
        for index_name in OBSOLETE_INDEX_NAMES:
            conn.execute(f"DROP INDEX IF EXISTS {index_name}")

    def _verify_integrity(self, conn: sqlite3.Connection):
        """验证数据库完整性"""
        logger.info("验证数据库完整性...")

        # 检查外键约束
        cursor = conn.execute("PRAGMA foreign_key_check")
        fk_violations = cursor.fetchall()
        if fk_violations:
            logger.warning("⚠️  发现外键约束违规: count=%s", len(fk_violations))
        else:
            logger.info("✓ 外键约束检查通过")

        # 检查表完整性
        cursor = conn.execute("PRAGMA integrity_check")
        integrity_result = cursor.fetchone()[0]
        if integrity_result == "ok":
            logger.info("✓ 数据库完整性检查通过")
        else:
            logger.warning("⚠️  数据库完整性检查失败: status=invalid")

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
            return self._insert_entry(conn, entry, file_path)

    def insert_entry_with_chunks(
        self,
        entry: Entry,
        file_path: str,
        chunks: list[str],
        *,
        operation_id: str | None = None,
        projection_sha256: str | None = None,
    ) -> int:
        """Atomically insert the core projection and optional commit proof.

        ``storage_operation_commits`` is written in the same SQLite transaction
        as the knowledge row, tags, FTS projection and chunks.  A caller that
        sees an exception after ``commit()`` can therefore distinguish a
        committed transaction from a failed one without guessing from
        ``file_path`` or another mutable business field.
        """

        self._validate_operation_proof(operation_id, projection_sha256)
        if operation_id is not None and projection_sha256 != entry_projection_sha256(
            entry, file_path, chunks
        ):
            raise ValueError("archive projection_sha256 与待提交内容不一致")

        with self.get_connection() as conn:
            knowledge_id = self._insert_entry(conn, entry, file_path)
            self._insert_chunks(conn, knowledge_id, chunks)
            if operation_id is not None and projection_sha256 is not None:
                self._record_storage_operation(
                    conn,
                    operation_id=operation_id,
                    action="archive",
                    knowledge_id=knowledge_id,
                    relative_file_path=file_path,
                    projection_sha256=projection_sha256,
                )
            return knowledge_id

    @staticmethod
    def _validate_operation_proof(
        operation_id: str | None,
        projection_sha256: str | None,
    ) -> None:
        if (operation_id is None) != (projection_sha256 is None):
            raise ValueError("operation_id 与 projection_sha256 必须同时提供")
        if operation_id is None:
            return
        if not operation_id or any(
            character not in "0123456789abcdef-" for character in operation_id
        ):
            raise ValueError("operation_id 非法")
        if (
            projection_sha256 is None
            or len(projection_sha256) != 64
            or any(character not in "0123456789abcdef" for character in projection_sha256)
        ):
            raise ValueError("projection_sha256 非法")

    @staticmethod
    def _record_storage_operation(
        conn: sqlite3.Connection,
        *,
        operation_id: str,
        action: str,
        knowledge_id: int,
        relative_file_path: str,
        projection_sha256: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO storage_operation_commits (
                operation_id, action, knowledge_id,
                relative_file_path, projection_sha256
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                action,
                knowledge_id,
                relative_file_path,
                projection_sha256,
            ),
        )

    def _insert_entry(
        self,
        conn: sqlite3.Connection,
        entry: Entry,
        file_path: str,
    ) -> int:
        """Insert one entry using the caller's transaction."""

        # 插入主表（使用原始数据，不分词）
        # FTS5 虚拟表会通过触发器自动同步并分词
        cursor = conn.execute("""
                INSERT INTO knowledge_items (
                    title, content, summary_one_sentence, summary_100_words,
                    keywords, tags, source_type, source_url, search_strategy,
                    file_path, word_count, event_time, published_at, archived_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry.title,
            entry.content,
            entry.summary_one_sentence,
            entry.summary_100_words,
            ",".join(entry.keywords) if isinstance(entry.keywords, list) else entry.keywords,
            ",".join(entry.tags) if isinstance(entry.tags, list) else entry.tags,
            entry.source_type,
            entry.source_url,
            entry.search_strategy,
            file_path,
            entry.word_count,
            entry.event_time,
            entry.published_at,
            entry.archived_at,
        ))

        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise sqlite3.IntegrityError("插入失败: lastrowid 为空")
        try:
            knowledge_id = int(lastrowid)
        except (TypeError, ValueError) as exc:
            raise sqlite3.IntegrityError("插入失败: lastrowid 非法") from exc
        keywords = (
            ",".join(entry.keywords)
            if isinstance(entry.keywords, list)
            else (entry.keywords or "")
        )
        tags = (
            ",".join(entry.tags)
            if isinstance(entry.tags, list)
            else (entry.tags or "")
        )
        fts5_data = self.text_processor.prepare_fts5_data(
            entry.title,
            entry.summary_100_words or "",
            keywords,
            tags,
        )
        conn.execute(f"DELETE FROM {FTS_TABLE_NAME} WHERE rowid = ?", (knowledge_id,))
        conn.execute(f"""
            INSERT INTO {FTS_TABLE_NAME}(rowid, title, summary_100_words, keywords, tags)
            VALUES (?, ?, ?, ?, ?)
        """, (
            knowledge_id,
            fts5_data["title"],
            fts5_data["summary_100_words"],
            fts5_data["keywords"],
            fts5_data["tags"],
        ))
        tag_values = entry.tags if isinstance(entry.tags, list) else [
            value.strip() for value in str(entry.tags or "").split(",") if value.strip()
        ]
        self._insert_tags(conn, knowledge_id, tag_values)
        logger.info(
            "插入知识条目: knowledge_id=%s source_type=%s content_length=%s",
            knowledge_id,
            entry.source_type,
            len(entry.content or ""),
        )
        return knowledge_id

    def _insert_tags(self, conn: sqlite3.Connection, knowledge_id: int, tags: list[str]):
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

    def insert_chunks(self, knowledge_id: int, chunks: list[str]) -> int:
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

        with self.get_connection() as conn:
            return self._insert_chunks(conn, knowledge_id, chunks)

    def _insert_chunks(
        self,
        conn: sqlite3.Connection,
        knowledge_id: int,
        chunks: list[str],
    ) -> int:
        """Insert chunks using the caller's transaction."""

        chunk_rows = []
        for chunk_index, chunk_text in enumerate(chunks):
            chunk_text_clean = (chunk_text or "").strip()
            if not chunk_text_clean:
                continue
            chunk_rows.append((knowledge_id, chunk_index, chunk_text_clean))

        if not chunk_rows:
            return 0

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

    def get_chunks_by_knowledge_id(self, knowledge_id: int) -> list[dict[str, Any]]:
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
    ) -> dict[str, Any] | None:
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

    def get_chunk_by_id(self, chunk_id: int) -> dict[str, Any] | None:
        """按持久化 chunk_id 查询单个分块。"""
        if chunk_id <= 0:
            raise ValueError("chunk_id 必须为正整数")

        with self.get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT chunk_id, knowledge_id, chunk_index, chunk_text,
                       context_before, context_after, section_title, created_at
                FROM content_chunks
                WHERE chunk_id = ?
                """,
                (chunk_id,),
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

    def count_chunks(self, knowledge_id: int | None = None) -> int:
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
            return row["cnt"] if row else 0

    def query_by_id(self, knowledge_id: int) -> dict[str, Any] | None:
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

    def query_by_file_path(self, file_path: str) -> dict[str, Any] | None:
        """Return the unique row for a Vault-relative Markdown path."""

        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_items WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            return dict(row) if row is not None else None

    def query_storage_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read a transaction-bound cross-store commit proof."""

        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT operation_id, action, knowledge_id,
                       relative_file_path, projection_sha256, committed_at
                FROM storage_operation_commits
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def query_r4_content_operation(self, operation_id: str) -> dict[str, Any] | None:
        """Read the revision-bound proof for one Q1′ apply_ai_patch commit."""

        with self.get_connection() as conn:
            row = conn.execute(
                """
                SELECT operation_id, action, knowledge_id, relative_file_path,
                       previous_revision_sha256, resulting_revision_sha256,
                       committed_at
                FROM r4_content_operation_commits
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def apply_ai_patch(
        self,
        *,
        operation_id: str,
        knowledge_id: int,
        relative_file_path: str,
        previous_revision_sha256: str,
        resulting_revision_sha256: str,
        entry: Entry,
    ) -> dict[str, Any]:
        """Atomically apply an already-validated R4 patch and write its proof.

        The caller has already updated a quarantined Markdown primary.  This
        transaction is the SQLite half of that Q1′ operation; it verifies the
        exact previous projection and records an operation-bound resulting
        revision so recovery never infers success from mutable business fields.
        """

        self._validate_operation_proof(operation_id, previous_revision_sha256)
        if not isinstance(relative_file_path, str) or not relative_file_path:
            raise ValueError("apply_ai_patch 必须提供 relative_file_path")
        if not isinstance(resulting_revision_sha256, str) or len(resulting_revision_sha256) != 64:
            raise ValueError("resulting_revision_sha256 无效")
        if type(knowledge_id) is not int or knowledge_id <= 0:
            raise ValueError("knowledge_id 必须是正整数")
        if not isinstance(entry, Entry):
            raise TypeError("apply_ai_patch entry 必须是 Entry")

        with self.get_connection() as conn:
            existing_proof = conn.execute(
                """
                SELECT operation_id, action, knowledge_id, relative_file_path,
                       previous_revision_sha256, resulting_revision_sha256
                FROM r4_content_operation_commits WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if existing_proof is not None:
                proof = dict(existing_proof)
                if proof != {
                    "operation_id": operation_id,
                    "action": "apply_ai_patch",
                    "knowledge_id": knowledge_id,
                    "relative_file_path": relative_file_path,
                    "previous_revision_sha256": previous_revision_sha256,
                    "resulting_revision_sha256": resulting_revision_sha256,
                }:
                    raise RuntimeError("apply_ai_patch 提交凭据与请求不一致")
                current = conn.execute(
                    "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                    (knowledge_id,),
                ).fetchone()
                if current is None:
                    raise RuntimeError("apply_ai_patch 凭据存在但知识条目缺失")
                chunks = [
                    dict(chunk)
                    for chunk in conn.execute(
                        """
                        SELECT chunk_index, chunk_text FROM content_chunks
                        WHERE knowledge_id = ? ORDER BY chunk_index ASC
                        """,
                        (knowledge_id,),
                    ).fetchall()
                ]
                if row_projection_sha256(dict(current), chunks) != resulting_revision_sha256:
                    raise RuntimeError("apply_ai_patch 凭据与当前 SQLite 投影不一致")
                return {
                    "knowledge_id": knowledge_id,
                    "relative_file_path": relative_file_path,
                    "resulting_revision_sha256": resulting_revision_sha256,
                }

            current = conn.execute(
                "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError("apply_ai_patch 目标知识条目不存在")
            current_data = dict(current)
            if current_data.get("file_path") != relative_file_path:
                raise RuntimeError("apply_ai_patch 目标 file_path 已变化")
            chunks = [
                dict(chunk)
                for chunk in conn.execute(
                    """
                    SELECT chunk_index, chunk_text FROM content_chunks
                    WHERE knowledge_id = ? ORDER BY chunk_index ASC
                    """,
                    (knowledge_id,),
                ).fetchall()
            ]
            if row_projection_sha256(current_data, chunks) != previous_revision_sha256:
                raise RuntimeError("apply_ai_patch 前 SQLite revision 已变化")

            self._decrement_tag_counts(conn, knowledge_id)
            conn.execute("DELETE FROM knowledge_tags WHERE knowledge_id = ?", (knowledge_id,))
            tags_value = ",".join(entry.tags) if isinstance(entry.tags, list) else str(entry.tags or "")
            conn.execute(
                """
                UPDATE knowledge_items
                SET summary_one_sentence = ?, summary_100_words = ?, tags = ?
                WHERE knowledge_id = ?
                """,
                (
                    entry.summary_one_sentence,
                    entry.summary_100_words,
                    tags_value,
                    knowledge_id,
                ),
            )
            self._insert_tags(conn, knowledge_id, list(entry.tags))
            updated = conn.execute(
                "SELECT * FROM knowledge_items WHERE knowledge_id = ?", (knowledge_id,)
            ).fetchone()
            assert updated is not None
            if row_projection_sha256(dict(updated), chunks) != resulting_revision_sha256:
                raise RuntimeError("apply_ai_patch 结果 SQLite projection 不一致")
            conn.execute(
                """
                INSERT INTO r4_content_operation_commits(
                    operation_id, action, knowledge_id, relative_file_path,
                    previous_revision_sha256, resulting_revision_sha256
                ) VALUES (?, 'apply_ai_patch', ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    knowledge_id,
                    relative_file_path,
                    previous_revision_sha256,
                    resulting_revision_sha256,
                ),
            )
            return {
                "knowledge_id": knowledge_id,
                "relative_file_path": relative_file_path,
                "resulting_revision_sha256": resulting_revision_sha256,
            }

    def delete_entry(
        self,
        knowledge_id: int,
        *,
        operation_id: str | None = None,
        projection_sha256: str | None = None,
        relative_file_path: str | None = None,
    ) -> bool:
        """删除知识条目及所有关联数据。

        级联删除 content_chunks、knowledge_tags、video_timestamps（外键 CASCADE）。
        FTS5 触发器自动清理全文索引。
        删除后递减相关标签计数，计数归零的标签自动清理。

        Args:
            knowledge_id: 知识条目 ID。

        Returns:
            True 表示成功删除，False 表示条目不存在。
        """
        self._validate_operation_proof(operation_id, projection_sha256)
        if operation_id is not None and not relative_file_path:
            raise ValueError("带提交凭据的删除必须提供 relative_file_path")

        with self.get_connection() as conn:
            if operation_id is not None:
                existing = conn.execute(
                    "SELECT * FROM knowledge_items WHERE knowledge_id = ?",
                    (knowledge_id,),
                ).fetchone()
                if existing is None:
                    return False
                if str(existing["file_path"]) != relative_file_path:
                    raise ValueError("删除提交凭据与 SQLite file_path 不一致")
                existing_chunks = [
                    dict(chunk)
                    for chunk in conn.execute(
                        """
                        SELECT chunk_index, chunk_text
                        FROM content_chunks
                        WHERE knowledge_id = ?
                        ORDER BY chunk_index ASC
                        """,
                        (knowledge_id,),
                    ).fetchall()
                ]
                if projection_sha256 != row_projection_sha256(
                    dict(existing), existing_chunks
                ):
                    raise RuntimeError("删除前 SQLite projection 已变化，拒绝提交")
            # 1. 先递减标签计数（必须在 CASCADE 删除 knowledge_tags 之前）
            self._decrement_tag_counts(conn, knowledge_id)

            # 2. 删除主表记录（CASCADE 自动清理 chunks/tags关联/timestamps，触发器清理 FTS5）
            cursor = conn.execute(
                "DELETE FROM knowledge_items WHERE knowledge_id = ?",
                (knowledge_id,),
            )

            deleted = cursor.rowcount > 0
            if deleted:
                if operation_id is not None and projection_sha256 is not None:
                    self._record_storage_operation(
                        conn,
                        operation_id=operation_id,
                        action="delete",
                        knowledge_id=knowledge_id,
                        relative_file_path=relative_file_path or "",
                        projection_sha256=projection_sha256,
                    )
                logger.info("删除知识条目完成")
            else:
                logger.warning("知识条目不存在，无法删除")
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

    def query_by_url(self, source_url: str) -> dict[str, Any] | None:
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
        except Exception as exc:
            logger.error("根据来源 URL 查询失败: error_type=%s", type(exc).__name__)
            raise

    def list_entries(
        self,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "archived_at",
        sort_order: str = "desc",
        source_type: str | None = None,
        tag: str | None = None
    ) -> list[dict[str, Any]]:
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
            params: list[Any] = []
            conditions: list[str] = []

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
        except Exception as exc:
            logger.error("获取知识条目列表失败: error_type=%s", type(exc).__name__)
            raise

    def count_entries(self, source_type: str | None = None, tag: str | None = None) -> int:
        """
        获取知识条目数量

        Args:
            source_type: 来源类型过滤
            tag: 标签过滤

        Returns:
            条目数量
        """
        try:
            params: list[Any] = []
            conditions: list[str] = []

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
                return row["cnt"] if row else 0
        except Exception as exc:
            logger.error("获取知识条目数量失败: error_type=%s", type(exc).__name__)
            raise

    def count_entries_by_source_type(self) -> list[tuple[str, int]]:
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
        except Exception as exc:
            logger.error("按来源类型统计失败: error_type=%s", type(exc).__name__)
            raise

    def get_all_tags_with_count(self, limit: int = 0) -> list[dict[str, Any]]:
        """
        获取全部标签及其计数

        Args:
            limit: 限制返回数量，0 表示不限制

        Returns:
            标签列表
        """
        try:
            query = "SELECT name, count FROM tags ORDER BY count DESC"
            params: list[Any] = []
            if limit > 0:
                query += " LIMIT ?"
                params.append(limit)

            with self.get_connection() as conn:
                cursor = conn.execute(query, tuple(params))
                rows = cursor.fetchall()
                return [{"name": row["name"], "count": row["count"]} for row in rows]
        except Exception as exc:
            logger.error("获取标签计数失败: error_type=%s", type(exc).__name__)
            raise

    def get_statistics(self) -> dict[str, Any]:
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
        except Exception as exc:
            logger.error("获取统计信息失败: error_type=%s", type(exc).__name__)
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
            logger.info("创建会话成功")
        except Exception as e:
            logger.error("创建会话失败: error_type=%s", type(e).__name__)
            raise

    def update_session(
        self,
        session_id: str,
        messages: list[dict],
        total_tokens: int,
        round_count: int,
        summary: str | None = None
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
            logger.info("更新会话成功")
        except Exception as e:
            logger.error("更新会话失败: error_type=%s", type(e).__name__)
            raise

    def get_session(self, session_id: str) -> dict[str, Any] | None:
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
            logger.error("获取会话失败: error_type=%s", type(e).__name__)
            raise

    def list_sessions(
        self,
        is_archived: bool = False,
        limit: int = 50
    ) -> list[dict[str, Any]]:
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
            logger.error("列出会话失败: error_type=%s", type(e).__name__)
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
            logger.info("删除会话成功")
        except Exception as e:
            logger.error("删除会话失败: error_type=%s", type(e).__name__)
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
            logger.info("%s会话成功", action)
        except Exception as e:
            logger.error("归档会话失败: error_type=%s", type(e).__name__)
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
            logger.info("关联会话与知识条目成功")
        except Exception as e:
            logger.error("关联会话失败: error_type=%s", type(e).__name__)
            raise

    def get_session_stats(self) -> dict[str, Any]:
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
            logger.error("获取会话统计失败: error_type=%s", type(e).__name__)
            raise

    def get_all_sessions_stats(self) -> list[dict[str, Any]]:
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
            logger.error("获取所有会话统计失败: error_type=%s", type(e).__name__)
            raise
