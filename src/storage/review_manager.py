"""
审核队列管理器

提供 ReviewItem 数据类和 ReviewManager 类，用于管理用户对 AI 生成内容的
审核流程（查看、修改摘要/标签、添加评论、AI 重新生成、通过/拒绝）。

所有方法为同步实现，由调用方使用 asyncio.to_thread() 包裹执行。
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.config import get_config
from src.utils.logger import get_logger
from src.storage.sqlite_connection import connect_existing_sqlite

logger = get_logger(__name__)


@dataclass
class ReviewItem:
    """
    审核队列条目数据类。

    存储 AI 生成的摘要、标签和用户修改后的版本，支持追踪审核状态与历史。
    """

    # AI 生成内容（必填）
    ai_generated_summary: str
    ai_generated_tags: str          # 逗号分隔字符串，如 "Python,AI,工具"
    source_type: str

    # 可选字段
    ai_cleaned_content: str = ""
    ai_generation_model: str = "deepseek-chat"
    original_content_preview: str = ""  # 内容前 500 字
    source_url: Optional[str] = None
    knowledge_id: Optional[int] = None

    # 用户审核内容
    user_summary: Optional[str] = None
    user_tags: Optional[str] = None         # 逗号分隔字符串
    user_comments: Optional[str] = None

    # AI 重新生成追踪
    regeneration_count: int = 0
    regeneration_prompts: str = "[]"        # JSON 数组字符串

    # 数据库主键与状态
    review_id: Optional[int] = None
    review_status: str = "pending"
    review_version: int = 1

    def get_effective_summary(self) -> str:
        """获取有效摘要：用户版本优先，否则使用 AI 版本。"""
        return self.user_summary or self.ai_generated_summary

    def get_effective_tags(self) -> List[str]:
        """获取有效标签列表：用户版本优先，否则使用 AI 版本。"""
        tags_str = self.user_tags or self.ai_generated_tags
        return [t.strip() for t in tags_str.split(",") if t.strip()]


class ReviewManager:
    """
    审核队列管理器。

    管理 review_queue 和 review_history 两张表，提供完整的审核生命周期操作：
    - 创建审核条目 (create_review)
    - 查询条目 (get_review)
    - 修改摘要/标签/评论 (update_user_summary / update_user_tags / add_user_comment)
    - AI 重新生成记录 (record_regeneration)
    - 通过/拒绝 (approve_review / reject_review)
    - 历史记录 (get_history)
    - 草稿管理 (list_drafts / restore_draft)
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        """
        初始化 ReviewManager。

        Args:
            db_path: 数据库文件路径，默认使用全局配置中的 db_path
        """
        if db_path is None:
            config = get_config()
            db_path = config.db_path
        self.db_path = Path(db_path)
        self._verify_tables()
        logger.info(f"ReviewManager 初始化: {self.db_path}")

    @contextmanager
    def _get_connection(self):
        """
        获取数据库连接（上下文管理器，与 SQLiteStore 保持一致的模式）。

        row_factory/PRAGMA 初始化任一步失败时也必须关闭连接，且恰好关闭一次。

        Yields:
            sqlite3.Connection: 自动提交或回滚的数据库连接
        """
        conn = connect_existing_sqlite(self.db_path)
        try:
            conn.row_factory = sqlite3.Row          # 支持字典式列访问
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            conn.close()
            raise
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"ReviewManager 数据库操作失败: {e}")
            raise
        finally:
            conn.close()

    def _verify_tables(self) -> None:
        """Require bootstrap/migrations to have created the review schema."""

        from src.runtime.errors import ErrorCode, PKVRuntimeError

        if not self.db_path.is_file():
            raise PKVRuntimeError(
                ErrorCode.DATABASE_MISSING,
                f"审核数据库尚未初始化: {self.db_path}",
            )
        conn = connect_existing_sqlite(self.db_path, read_only=True)
        try:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        missing = {"review_queue", "review_history"} - tables
        if missing:
            raise PKVRuntimeError(
                ErrorCode.DATABASE_SCHEMA_DRIFT,
                f"审核表未由 migration 创建: {', '.join(sorted(missing))}",
            )

    # ------------------------------------------------------------------
    # CRUD 操作
    # ------------------------------------------------------------------

    def create_review(self, item: ReviewItem) -> int:
        """
        创建审核队列条目。

        Args:
            item: ReviewItem 数据对象

        Returns:
            新建记录的 review_id

        Raises:
            ValueError: item 数据不合法
            sqlite3.Error: 数据库操作失败
        """
        if not item.ai_generated_summary:
            raise ValueError("ai_generated_summary 不能为空")

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO review_queue (
                    ai_generated_summary, ai_generated_tags, source_type,
                    ai_cleaned_content, ai_generation_model,
                    original_content_preview, source_url, knowledge_id,
                    user_summary, user_tags, user_comments,
                    regeneration_count, regeneration_prompts,
                    review_status, review_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.ai_generated_summary,
                    item.ai_generated_tags,
                    item.source_type,
                    item.ai_cleaned_content,
                    item.ai_generation_model,
                    item.original_content_preview,
                    item.source_url,
                    item.knowledge_id,
                    item.user_summary,
                    item.user_tags,
                    item.user_comments,
                    item.regeneration_count,
                    item.regeneration_prompts,
                    item.review_status,
                    item.review_version,
                ),
            )
            review_id = cursor.lastrowid
            self._add_history(
                conn,
                review_id,
                action="create",
                details=json.dumps(
                    {
                        "source_type": item.source_type,
                        "ai_tags": item.ai_generated_tags,
                        "summary_length": len(item.ai_generated_summary),
                    },
                    ensure_ascii=False,
                ),
                operator="system",
            )

        logger.info(f"审核条目已创建: review_id={review_id}")
        return review_id

    def get_review(self, review_id: int) -> Optional[ReviewItem]:
        """
        按 review_id 查询审核条目。

        Args:
            review_id: 审核条目 ID

        Returns:
            ReviewItem 对象，不存在时返回 None
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM review_queue WHERE review_id = ?", (review_id,)
            ).fetchone()

        if row is None:
            logger.debug(f"审核条目不存在: review_id={review_id}")
            return None

        return self._row_to_item(row)

    def update_user_summary(self, review_id: int, summary: str) -> bool:
        """
        更新用户修改的摘要。

        Args:
            review_id: 审核条目 ID
            summary: 用户修改后的摘要

        Returns:
            True 表示更新成功，False 表示条目不存在
        """
        ok = self._update_field(
            review_id,
            sql="UPDATE review_queue SET user_summary = ?, review_version = review_version + 1 WHERE review_id = ?",
            params=(summary, review_id),
            action="edit_summary",
            details=json.dumps({"summary_length": len(summary)}, ensure_ascii=False),
            warn_msg=f"审核条目不存在，无法更新摘要: review_id={review_id}",
        )
        if ok:
            logger.debug(f"用户摘要已更新: review_id={review_id}")
        return ok

    def update_user_tags(self, review_id: int, tags: List[str]) -> bool:
        """
        更新用户修改的标签。

        Args:
            review_id: 审核条目 ID
            tags: 标签列表（将转换为逗号分隔字符串存储）

        Returns:
            True 表示更新成功，False 表示条目不存在
        """
        tags_str = ",".join(t.strip() for t in tags if t.strip())
        ok = self._update_field(
            review_id,
            sql="UPDATE review_queue SET user_tags = ?, review_version = review_version + 1 WHERE review_id = ?",
            params=(tags_str, review_id),
            action="edit_tags",
            details=json.dumps({"tags": tags, "tags_str": tags_str}, ensure_ascii=False),
            warn_msg=f"审核条目不存在，无法更新标签: review_id={review_id}",
        )
        if ok:
            logger.debug(f"用户标签已更新: review_id={review_id}, tags={tags_str}")
        return ok

    def add_user_comment(self, review_id: int, comment: str) -> bool:
        """
        追加用户评论（在已有评论后换行追加）。

        Args:
            review_id: 审核条目 ID
            comment: 用户评论内容

        Returns:
            True 表示追加成功，False 表示条目不存在
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT user_comments FROM review_queue WHERE review_id = ?",
                (review_id,),
            ).fetchone()

            if row is None:
                logger.warning(f"审核条目不存在，无法添加评论: review_id={review_id}")
                return False

            existing = row["user_comments"] or ""
            new_comments = f"{existing}\n{comment}".strip() if existing else comment

            conn.execute(
                """
                UPDATE review_queue
                SET user_comments = ?, review_version = review_version + 1
                WHERE review_id = ?
                """,
                (new_comments, review_id),
            )
            self._add_history(
                conn,
                review_id,
                action="add_comment",
                details=json.dumps({"comment": comment[:200]}, ensure_ascii=False),
            )

        logger.debug(f"用户评论已追加: review_id={review_id}")
        return True

    def record_regeneration(
        self,
        review_id: int,
        prompt: str,
        new_summary: str,
        new_tags: List[str],
    ) -> bool:
        """
        记录一次 AI 重新生成操作，并更新 AI 生成内容。

        Args:
            review_id: 审核条目 ID
            prompt: 用户给 AI 的指导 prompt
            new_summary: 重新生成的摘要
            new_tags: 重新生成的标签列表

        Returns:
            True 表示记录成功，False 表示条目不存在
        """
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT regeneration_count, regeneration_prompts FROM review_queue WHERE review_id = ?",
                (review_id,),
            ).fetchone()

            if row is None:
                logger.warning(f"审核条目不存在，无法记录重生成: review_id={review_id}")
                return False

            count = (row["regeneration_count"] or 0) + 1
            try:
                prompts_list = json.loads(row["regeneration_prompts"] or "[]")
            except json.JSONDecodeError:
                prompts_list = []
            prompts_list.append({"index": count, "prompt": prompt})

            new_tags_str = ",".join(t.strip() for t in new_tags if t.strip())

            conn.execute(
                """
                UPDATE review_queue
                SET ai_generated_summary = ?,
                    ai_generated_tags = ?,
                    regeneration_count = ?,
                    regeneration_prompts = ?,
                    review_version = review_version + 1
                WHERE review_id = ?
                """,
                (
                    new_summary,
                    new_tags_str,
                    count,
                    json.dumps(prompts_list, ensure_ascii=False),
                    review_id,
                ),
            )
            self._add_history(
                conn,
                review_id,
                action="regenerate",
                details=json.dumps(
                    {
                        "prompt": prompt[:200],
                        "regeneration_index": count,
                        "new_summary_length": len(new_summary),
                        "new_tags": new_tags,
                    },
                    ensure_ascii=False,
                ),
                operator="system",
            )

        logger.info(f"AI 重新生成已记录: review_id={review_id}, count={count}")
        return True

    def approve_review(self, review_id: int) -> bool:
        """
        通过审核（将状态改为 approved）。

        Args:
            review_id: 审核条目 ID

        Returns:
            True 表示操作成功，False 表示条目不存在
        """
        with self._get_connection() as conn:
            result = conn.execute(
                "UPDATE review_queue SET review_status = 'approved' WHERE review_id = ?",
                (review_id,),
            )
            if result.rowcount == 0:
                logger.warning(f"审核条目不存在，无法通过: review_id={review_id}")
                return False
            self._add_history(conn, review_id, action="approve", details=json.dumps({}))

        logger.info(f"审核已通过: review_id={review_id}")
        return True

    def reject_review(self, review_id: int) -> bool:
        """
        拒绝审核（将状态改为 draft，内容保存到草稿区）。

        Args:
            review_id: 审核条目 ID

        Returns:
            True 表示操作成功，False 表示条目不存在
        """
        with self._get_connection() as conn:
            result = conn.execute(
                "UPDATE review_queue SET review_status = 'draft' WHERE review_id = ?",
                (review_id,),
            )
            if result.rowcount == 0:
                logger.warning(f"审核条目不存在，无法拒绝: review_id={review_id}")
                return False
            self._add_history(
                conn,
                review_id,
                action="reject",
                details=json.dumps({"moved_to": "draft"}, ensure_ascii=False),
            )

        logger.info(f"审核已拒绝，存入草稿区: review_id={review_id}")
        return True

    def get_history(self, review_id: int) -> List[Dict[str, Any]]:
        """
        获取指定审核条目的完整操作历史。

        Args:
            review_id: 审核条目 ID

        Returns:
            历史记录列表，每条记录为字典格式（按时间正序排列）
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT history_id, review_id, action, details, operator, created_at
                FROM review_history
                WHERE review_id = ?
                ORDER BY history_id ASC
                """,
                (review_id,),
            ).fetchall()

        result = []
        for row in rows:
            try:
                details_obj = json.loads(row["details"] or "{}")
            except json.JSONDecodeError:
                details_obj = {"raw": row["details"]}
            result.append(
                {
                    "history_id": row["history_id"],
                    "review_id": row["review_id"],
                    "action": row["action"],
                    "details": details_obj,
                    "operator": row["operator"],
                    "created_at": row["created_at"],
                }
            )
        return result

    def list_drafts(self) -> List[ReviewItem]:
        """
        列出所有草稿区条目（status = 'draft'）。

        Returns:
            草稿 ReviewItem 列表（按创建时间倒序排列）
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_queue
                WHERE review_status = 'draft'
                ORDER BY created_at DESC
                """,
            ).fetchall()

        return [self._row_to_item(row) for row in rows]

    def restore_draft(self, review_id: int) -> bool:
        """
        将草稿恢复为待审核状态（status 从 draft 改回 pending）。

        Args:
            review_id: 审核条目 ID

        Returns:
            True 表示恢复成功，False 表示条目不存在或状态不是 draft
        """
        ok = self._update_field(
            review_id,
            sql="UPDATE review_queue SET review_status = 'pending', review_version = review_version + 1 WHERE review_id = ? AND review_status = 'draft'",
            params=(review_id,),
            action="restore",
            details=json.dumps({"from_status": "draft", "to_status": "pending"}, ensure_ascii=False),
            warn_msg=f"无法恢复草稿: review_id={review_id}（不存在或状态不是 draft）",
        )
        if ok:
            logger.info(f"草稿已恢复为待审核: review_id={review_id}")
        return ok

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _update_field(
        self,
        review_id: int,
        sql: str,
        params: tuple,
        action: str,
        details: str,
        warn_msg: str,
        operator: str = "user",
    ) -> bool:
        """
        执行单字段更新并写入历史记录的通用模板。

        只要调用方遵循"UPDATE ... WHERE review_id = ?"的约定，rowcount 为 0
        即表示条目不存在，统一返回 False 并记录警告。
        """
        with self._get_connection() as conn:
            result = conn.execute(sql, params)
            if result.rowcount == 0:
                logger.warning(warn_msg)
                return False
            self._add_history(conn, review_id, action=action, details=details, operator=operator)
        return True

    def _add_history(
        self,
        conn: sqlite3.Connection,
        review_id: int,
        action: str,
        details: str,
        operator: str = "user",
    ) -> None:
        """
        在已有连接中写入历史记录（内部方法，需在事务中调用）。

        Args:
            conn: 当前数据库连接
            review_id: 关联的审核条目 ID
            action: 操作类型（如 create, edit_summary, approve 等）
            details: 操作详情（JSON 字符串）
            operator: 操作人（user 或 system）
        """
        conn.execute(
            """
            INSERT INTO review_history (review_id, action, details, operator)
            VALUES (?, ?, ?, ?)
            """,
            (review_id, action, details, operator),
        )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ReviewItem:
        """将数据库行转换为 ReviewItem 数据对象。"""
        return ReviewItem(
            review_id=row["review_id"],
            ai_generated_summary=row["ai_generated_summary"],
            ai_generated_tags=row["ai_generated_tags"] or "",
            source_type=row["source_type"] or "unknown",
            ai_cleaned_content=row["ai_cleaned_content"] or "",
            ai_generation_model=row["ai_generation_model"] or "deepseek-chat",
            original_content_preview=row["original_content_preview"] or "",
            source_url=row["source_url"],
            knowledge_id=row["knowledge_id"],
            user_summary=row["user_summary"],
            user_tags=row["user_tags"],
            user_comments=row["user_comments"],
            regeneration_count=row["regeneration_count"] or 0,
            regeneration_prompts=row["regeneration_prompts"] or "[]",
            review_status=row["review_status"] or "pending",
            review_version=row["review_version"] or 1,
        )
