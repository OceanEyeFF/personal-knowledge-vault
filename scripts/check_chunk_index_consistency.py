"""
Chunk 文本与向量索引一致性检查工具。

默认只读，不修改数据库和索引文件。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.utils.config import Config
from src.storage.sqlite_connection import connect_existing_sqlite


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PKV Chunk 索引一致性检查工具")
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="覆盖 SQLite 路径（默认读取配置）",
    )
    parser.add_argument(
        "--vector-dir",
        type=str,
        default=None,
        help="覆盖向量索引目录（默认读取配置）",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default=None,
        help="输出 JSON 报告（传入文件路径或 '-' 输出到 stdout）",
    )
    return parser.parse_args()


def _write_report(path: str, content: str) -> None:
    if path == "-":
        print("\n" + content)
        return
    Path(path).write_text(content, encoding="utf-8")


def _load_vector_mappings(vector_dir: Path) -> set[tuple[int, int]]:
    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    if not metadata_path.exists():
        return set()

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    mappings = set()
    for mapping in metadata.get("id_mapping", {}).values():
        if not isinstance(mapping, list) or len(mapping) != 2:
            continue
        mappings.add((int(mapping[0]), int(mapping[1])))
    return mappings


def _load_db_chunks(db_path: Path) -> set[tuple[int, int]]:
    """Read an existing database without allowing SQLite to create one."""

    conn = connect_existing_sqlite(Path(db_path), read_only=True)
    try:
        cursor = conn.execute(
            """
            SELECT knowledge_id, chunk_index
            FROM content_chunks
            """
        )
        return {(int(row[0]), int(row[1])) for row in cursor.fetchall()}
    finally:
        conn.close()


def main() -> int:
    args = _parse_args()
    config = Config()

    db_path = Path(args.db_path) if args.db_path else config.db_path
    vector_dir = Path(args.vector_dir) if args.vector_dir else config.vector_index_dir

    try:
        db_chunks = _load_db_chunks(db_path)
        vector_chunks = _load_vector_mappings(vector_dir)
    except Exception:
        print(
            "一致性检查被拒绝：数据库必须已存在且能以安全只读方式打开；"
            "本次未创建数据库或索引文件。",
            file=sys.stderr,
        )
        return 2

    missing_in_vector = sorted(db_chunks - vector_chunks)
    missing_in_sqlite = sorted(vector_chunks - db_chunks)

    report = {
        "db_path": str(db_path),
        "vector_dir": str(vector_dir),
        "sqlite_chunk_count": len(db_chunks),
        "vector_chunk_count": len(vector_chunks),
        "missing_in_vector": [
            {"knowledge_id": knowledge_id, "chunk_index": chunk_index}
            for knowledge_id, chunk_index in missing_in_vector
        ],
        "missing_in_sqlite": [
            {"knowledge_id": knowledge_id, "chunk_index": chunk_index}
            for knowledge_id, chunk_index in missing_in_sqlite
        ],
    }

    print("=" * 70)
    print(" PKV Chunk 索引一致性检查")
    print("=" * 70)
    print(f"数据库路径: {db_path}")
    print(f"向量目录: {vector_dir}")
    print(f"SQLite chunks: {len(db_chunks)}")
    print(f"Vector chunks: {len(vector_chunks)}")
    print(f"缺失向量: {len(missing_in_vector)}")
    print(f"缺失文本: {len(missing_in_sqlite)}")

    if missing_in_vector[:10]:
        print("\n缺失向量样例:")
        for knowledge_id, chunk_index in missing_in_vector[:10]:
            print(f"  - knowledge_id={knowledge_id}, chunk_index={chunk_index}")

    if missing_in_sqlite[:10]:
        print("\n缺失文本样例:")
        for knowledge_id, chunk_index in missing_in_sqlite[:10]:
            print(f"  - knowledge_id={knowledge_id}, chunk_index={chunk_index}")

    if args.report_json:
        _write_report(
            args.report_json,
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
