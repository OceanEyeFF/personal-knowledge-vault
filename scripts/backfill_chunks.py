"""
已停用的 Chunk 回填脚本。

保留 ``run_chunk_backfill`` 仅供隔离合成 fixture 使用；裸脚本入口不再
提供绕过当前 lifecycle / writer lease 的维护写入路径。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts._legacy_maintenance import reject_legacy_maintenance_entrypoint

if TYPE_CHECKING:
    import numpy as np

    from src.ai.embedder import Embedder
    from src.storage.sqlite_store import SQLiteStore


@dataclass
class ChunkBackfillCandidate:
    knowledge_id: int
    title: str
    reasons: list[str] = field(default_factory=list)


@dataclass
class ChunkBackfillReport:
    mode: str
    db_path: Path
    vector_index_dir: Path
    scanned_entries: int = 0
    candidate_entries: int = 0
    applied_entries: int = 0
    skipped_complete_entries: int = 0
    blocked_entries: int = 0
    failed_entries: int = 0
    candidates: list[ChunkBackfillCandidate] = field(default_factory=list)
    blocked_details: list[str] = field(default_factory=list)
    failed_details: list[str] = field(default_factory=list)


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="已停用的 PKV Chunk 回填入口（所有执行均会被拒绝）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="历史参数；入口已停用，不会执行回填",
    )
    parser.add_argument(
        "--knowledge-id",
        action="append",
        type=int,
        dest="knowledge_ids",
        help="历史参数；入口已停用",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=None,
        help="历史参数；入口已停用",
    )
    return parser.parse_args(argv)


def _normalize_knowledge_ids(knowledge_ids: Optional[Sequence[int]]) -> list[int]:
    if not knowledge_ids:
        return []
    return list(dict.fromkeys(int(knowledge_id) for knowledge_id in knowledge_ids))


def _require_isolated_fixture_paths(*, db_path: Path, vector_index_dir: Path) -> None:
    """Restrict the retained helper to the wrapper-selected test root."""

    data_root = os.environ.get("DATA_DIR")
    if os.environ.get("PKV_TEST_OFFLINE") != "1" or not data_root:
        raise RuntimeError(
            "run_chunk_backfill 仅可用于 run-test.ps1 选择的隔离合成 fixture"
        )

    root = Path(data_root).resolve(strict=False)
    for label, path in (("db_path", db_path), ("vector_index_dir", vector_index_dir)):
        candidate = Path(path).resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise RuntimeError(
                "run_chunk_backfill 仅可操作本次 DATA_DIR 下的隔离合成 fixture: "
                f"{label}"
            )


def _load_entry_rows(store: SQLiteStore, knowledge_ids: Sequence[int]) -> list[dict]:
    with store.get_connection() as conn:
        if knowledge_ids:
            placeholders = ",".join("?" for _ in knowledge_ids)
            cursor = conn.execute(
                f"""
                SELECT knowledge_id, title, content
                FROM knowledge_items
                WHERE knowledge_id IN ({placeholders})
                ORDER BY knowledge_id ASC
                """,
                tuple(knowledge_ids),
            )
        else:
            cursor = conn.execute(
                """
                SELECT knowledge_id, title, content
                FROM knowledge_items
                ORDER BY knowledge_id ASC
                """
            )
        rows = cursor.fetchall()
    return [dict(row) for row in rows]


def _load_vector_index_dim(index_dir: Path, name: str) -> Optional[int]:
    metadata_path = Path(index_dir) / f"{name}_metadata.json"
    if not metadata_path.exists():
        return None

    with open(metadata_path, "r", encoding="utf-8") as file:
        metadata = json.load(file)
    dim = metadata.get("dim")
    return int(dim) if dim is not None else None


def _validate_vector_index_dim(index_dir: Path, embedding_dim: int) -> None:
    for index_name in ("doc_vectors", "chunk_vectors"):
        existing_dim = _load_vector_index_dim(index_dir, index_name)
        if existing_dim is None:
            continue
        if existing_dim != embedding_dim:
            raise RuntimeError(
                f"{index_name} 维度不匹配: existing={existing_dim}, current={embedding_dim}。"
                "当前脚本不会自动重建索引，请先人工确认索引维度与 embedding 配置。"
            )


def _resolve_embedder_dim(embedder: Embedder) -> int:
    """解析当前 embedder 的实际向量维度。"""
    embedder_dim = getattr(embedder, "dim", None)
    if embedder_dim is not None:
        return int(embedder_dim)

    if hasattr(embedder, "resolve_dim"):
        return int(embedder.resolve_dim())

    raise RuntimeError("当前 embedder 无法提供向量维度，请显式传入 embedding_dim")


def _load_chunk_vector_indices(index_dir: Path, knowledge_id: int) -> list[int]:
    import hnswlib

    metadata_path = Path(index_dir) / "chunk_vectors_metadata.json"
    index_path = Path(index_dir) / "chunk_vectors.idx"
    if not metadata_path.exists() or not index_path.exists():
        return []

    with open(metadata_path, "r", encoding="utf-8") as file:
        metadata = json.load(file)

    dim = metadata.get("dim")
    if dim is None:
        return []

    index = hnswlib.Index(space="cosine", dim=int(dim))
    index.load_index(str(index_path), allow_replace_deleted=True)

    chunk_indices = []
    for hnswlib_id, mapping in metadata.get("id_mapping", {}).items():
        if int(mapping[0]) != knowledge_id:
            continue
        try:
            vectors = index.get_items([int(hnswlib_id)])
        except RuntimeError:
            continue
        if vectors is None or len(vectors) == 0:
            continue
        chunk_indices.append(int(mapping[1]))
    return sorted(chunk_indices)


def _build_candidate_reasons(
    chunk_rows: list[dict],
    vector_chunk_indices: list[int],
    expected_chunks: list[str],
) -> list[str]:
    reasons: list[str] = []
    sorted_chunk_rows = sorted(chunk_rows, key=lambda row: int(row["chunk_index"]))
    chunk_indices = [int(row["chunk_index"]) for row in sorted_chunk_rows]
    stored_chunks = [str(row.get("chunk_text") or "").strip() for row in sorted_chunk_rows]
    expected_chunk_indices = list(range(len(expected_chunks)))

    if not chunk_rows:
        reasons.append("missing_chunks")
    elif chunk_indices != expected_chunk_indices:
        reasons.append("irregular_chunk_rows")
    elif expected_chunks and stored_chunks != expected_chunks:
        reasons.append("stale_chunk_rows")

    if chunk_rows and sorted(vector_chunk_indices) != expected_chunk_indices:
        reasons.append("chunk_vector_mismatch")
    elif not chunk_rows and vector_chunk_indices:
        reasons.append("orphan_chunk_vectors")

    return reasons


def _normalize_chunk_payload(
    chunk_vectors: np.ndarray,
    chunks: Optional[list[str]],
) -> tuple[np.ndarray, list[str]]:
    import numpy as np

    if chunk_vectors is None or chunks is None:
        raise ValueError("embed_chunks 必须同时返回 chunk_vectors 和 chunks")
    if chunk_vectors.ndim != 2:
        raise ValueError("chunk_vectors 必须是二维矩阵")
    if chunk_vectors.shape[0] != len(chunks):
        raise ValueError("chunk_vectors 与 chunks 数量不一致")

    normalized_chunks: list[str] = []
    normalized_vectors: list[np.ndarray] = []
    for index, chunk_text in enumerate(chunks):
        chunk_text_clean = (chunk_text or "").strip()
        if not chunk_text_clean:
            continue
        normalized_chunks.append(chunk_text_clean)
        normalized_vectors.append(chunk_vectors[index])

    if not normalized_chunks:
        raise ValueError("当前条目无法生成有效 chunk")

    normalized_vector_matrix = np.vstack(normalized_vectors).astype("float32")
    return normalized_vector_matrix, normalized_chunks


def _normalize_expected_chunks(
    content: str,
    embedder: Optional[Embedder] = None,
) -> list[str]:
    """按当前切块规则生成并清洗期望 chunk 文本。"""
    if not content or not content.strip():
        return []

    if embedder is not None and hasattr(embedder, "split_chunks"):
        raw_chunks = getattr(embedder, "split_chunks")(content)
    else:
        from src.utils.text_utils import split_text_into_chunks

        raw_chunks = split_text_into_chunks(content)

    normalized_chunks: list[str] = []
    for chunk_text in raw_chunks:
        chunk_text_clean = (chunk_text or "").strip()
        if chunk_text_clean:
            normalized_chunks.append(chunk_text_clean)
    return normalized_chunks


def run_chunk_backfill(
    *,
    db_path: Path,
    vector_index_dir: Path,
    knowledge_ids: Optional[Sequence[int]] = None,
    apply: bool = False,
    embedding_dim: Optional[int] = 1536,
    embedder: Optional[Embedder] = None,
) -> ChunkBackfillReport:
    """Fixture-only compatibility helper for isolated synthetic paths.

    This function is deliberately not exposed through the raw script entrypoint.
    Product maintenance must use the confirmed runtime lifecycle instead.
    """

    _require_isolated_fixture_paths(
        db_path=Path(db_path),
        vector_index_dir=Path(vector_index_dir),
    )
    from src.ai.embedder import Embedder
    from src.storage.sqlite_store import SQLiteStore
    from src.storage.vector_store import VectorStore

    store = SQLiteStore(Path(db_path))
    active_embedder = embedder or (Embedder() if apply else None)
    target_knowledge_ids = _normalize_knowledge_ids(knowledge_ids)
    rows = _load_entry_rows(store, target_knowledge_ids)
    report = ChunkBackfillReport(
        mode="apply" if apply else "dry-run",
        db_path=Path(db_path),
        vector_index_dir=Path(vector_index_dir),
        scanned_entries=len(rows),
    )

    candidates: list[tuple[dict, list[str]]] = []
    for row in rows:
        knowledge_id = int(row["knowledge_id"])
        chunk_rows = store.get_chunks_by_knowledge_id(knowledge_id)
        vector_chunk_indices = _load_chunk_vector_indices(
            Path(vector_index_dir), knowledge_id
        )
        expected_chunks = _normalize_expected_chunks(
            str(row.get("content") or ""),
            active_embedder,
        )
        reasons = _build_candidate_reasons(
            chunk_rows,
            vector_chunk_indices,
            expected_chunks,
        )
        if reasons:
            candidates.append((row, reasons))
            report.candidates.append(
                ChunkBackfillCandidate(
                    knowledge_id=knowledge_id,
                    title=row["title"],
                    reasons=reasons,
                )
            )
        else:
            report.skipped_complete_entries += 1

    report.candidate_entries = len(candidates)
    if not apply:
        if embedding_dim is not None:
            _validate_vector_index_dim(Path(vector_index_dir), embedding_dim)
        return report

    if active_embedder is None:
        active_embedder = Embedder()
    resolved_embedding_dim = embedding_dim
    if resolved_embedding_dim is None:
        resolved_embedding_dim = _resolve_embedder_dim(active_embedder)

    _validate_vector_index_dim(Path(vector_index_dir), resolved_embedding_dim)
    vector_store = VectorStore(Path(vector_index_dir), dim=resolved_embedding_dim)
    for row, reasons in candidates:
        knowledge_id = int(row["knowledge_id"])
        title = row["title"]
        content = (row.get("content") or "").strip()

        if not content:
            report.blocked_entries += 1
            report.blocked_details.append(
                f"knowledge_id={knowledge_id}, title={title}: content 为空，无法回填"
            )
            continue

        try:
            raw_vectors, raw_chunks = active_embedder.embed_chunks(content, True)
            chunk_vectors, chunks = _normalize_chunk_payload(raw_vectors, raw_chunks)
            if len(chunks) - 1 > VectorStore.MAX_CHUNK_INDEX:
                raise ValueError(
                    f"chunk 数超出编码上限: {len(chunks)} > {VectorStore.MAX_CHUNK_INDEX + 1}"
                )

            expected_indices = list(range(len(chunks)))
            existing_chunk_rows = store.get_chunks_by_knowledge_id(knowledge_id)
            existing_chunk_texts = [row["chunk_text"] for row in existing_chunk_rows]
            existing_vector_indices = vector_store.get_chunk_indices_for_entry(knowledge_id)

            chunk_rewrite_needed = existing_chunk_texts != chunks
            vector_rewrite_needed = (
                chunk_rewrite_needed
                or sorted(existing_vector_indices) != expected_indices
            )

            if chunk_rewrite_needed and existing_chunk_rows:
                store.delete_chunks_by_knowledge_id(knowledge_id)
            if chunk_rewrite_needed or not existing_chunk_rows:
                store.insert_chunks(knowledge_id, chunks)

            if vector_rewrite_needed and existing_vector_indices:
                vector_store.delete_chunk_vectors_for_entry(knowledge_id)
            if vector_rewrite_needed or not existing_vector_indices:
                vector_store.add_chunk_vectors(
                    knowledge_id=knowledge_id,
                    chunk_indices=expected_indices,
                    vectors=chunk_vectors,
                    replace_deleted=bool(existing_vector_indices),
                )

            report.applied_entries += 1
        except Exception as exc:
            report.failed_entries += 1
            report.failed_details.append(
                f"knowledge_id={knowledge_id}, title={title}, reasons={','.join(reasons)}: {exc}"
            )

    return report


def _print_report(report: ChunkBackfillReport) -> None:
    print("=" * 70)
    print(f" PKV Chunk 回填 {report.mode.upper()}")
    print("=" * 70)
    print(f"数据库路径: {report.db_path}")
    print(f"向量索引目录: {report.vector_index_dir}")
    print(f"扫描条目数: {report.scanned_entries}")
    print(f"候选条目数: {report.candidate_entries}")
    print(f"已完整跳过: {report.skipped_complete_entries}")

    if report.candidates[:10]:
        print("\n待处理样例:")
        for candidate in report.candidates[:10]:
            reasons = ", ".join(candidate.reasons)
            print(
                f"  - knowledge_id={candidate.knowledge_id}, "
                f"title={candidate.title}, reasons={reasons}"
            )

    if report.mode == "apply":
        print("\n执行结果:")
        print(f"  - 应用成功: {report.applied_entries}")
        print(f"  - 内容阻塞: {report.blocked_entries}")
        print(f"  - 执行失败: {report.failed_entries}")

    if report.blocked_details:
        print("\n阻塞明细:")
        for detail in report.blocked_details[:10]:
            print(f"  - {detail}")

    if report.failed_details:
        print("\n失败明细:")
        for detail in report.failed_details[:10]:
            print(f"  - {detail}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Keep argparse help available, but reject every real invocation before
    # Config() could select a user data root.  The helper above remains a
    # fixture seam only; it is not an alternate lifecycle implementation.
    _parse_args(argv)
    return reject_legacy_maintenance_entrypoint("scripts/backfill_chunks.py")


if __name__ == "__main__":
    raise SystemExit(main())
