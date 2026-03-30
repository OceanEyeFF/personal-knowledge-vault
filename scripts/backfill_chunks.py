"""
Chunk 回填入口。

Phase 1 仅支持 dry-run 估算，不执行真实 embedding 回填。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.storage.sqlite_store import SQLiteStore
from src.utils.config import Config


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PKV Chunk 回填入口")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="预留真实回填开关；Phase 1 暂不支持",
    )
    parser.add_argument(
        "--knowledge-id",
        action="append",
        type=int,
        dest="knowledge_ids",
        help="仅统计指定 knowledge_id，可重复传入",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="覆盖 SQLite 路径（默认读取配置）",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.apply:
        print("Phase 1 暂不支持真实回填，请先使用 dry-run 评估范围。")
        return 2

    config = Config()
    db_path = Path(args.db_path) if args.db_path else config.db_path
    store = SQLiteStore(db_path)

    knowledge_ids = args.knowledge_ids or []
    with store.get_connection() as conn:
        if knowledge_ids:
            placeholders = ",".join("?" for _ in knowledge_ids)
            cursor = conn.execute(
                f"""
                SELECT knowledge_id, title
                FROM knowledge_items
                WHERE knowledge_id IN ({placeholders})
                  AND knowledge_id NOT IN (
                      SELECT DISTINCT knowledge_id FROM content_chunks
                  )
                ORDER BY knowledge_id ASC
                """,
                tuple(knowledge_ids),
            )
        else:
            cursor = conn.execute(
                """
                SELECT knowledge_id, title
                FROM knowledge_items
                WHERE knowledge_id NOT IN (
                    SELECT DISTINCT knowledge_id FROM content_chunks
                )
                ORDER BY knowledge_id ASC
                """
            )
        rows = cursor.fetchall()

    print("=" * 70)
    print(" PKV Chunk 回填 Dry-Run")
    print("=" * 70)
    print(f"数据库路径: {db_path}")
    print(f"待回填条目数: {len(rows)}")

    if rows[:10]:
        print("\n待回填样例:")
        for row in rows[:10]:
            print(f"  - knowledge_id={row['knowledge_id']}, title={row['title']}")

    print("\n提示: Phase 1 仅定义回填入口与范围统计，未启用真实 embedding 回填。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
