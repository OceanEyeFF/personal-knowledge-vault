"""
Chunk 回填安全性集成测试
"""

import json
import sys
from pathlib import Path

import numpy as np

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from scripts.backfill_chunks import run_chunk_backfill
from src.storage.markdown_store import MarkdownStore, Entry
from src.storage.sqlite_store import SQLiteStore
from src.storage.vector_store import VectorStore


class DeterministicEmbedder:
    """用于回填测试的确定性 Embedder。"""

    dim = 8

    def embed_document(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        if "alpha" in (text or "").lower():
            vector[0] = 1.0
        else:
            vector[1] = 1.0
        return vector

    def embed_chunks(
        self, text: str, return_chunks: bool = False
    ) -> tuple[np.ndarray, list[str] | None]:
        chunks = self.split_chunks(text)
        vectors = np.vstack([self.embed_document(chunk) for chunk in chunks]).astype(
            np.float32
        )
        return vectors, chunks if return_chunks else None

    def split_chunks(self, text: str) -> list[str]:
        return [chunk.strip() for chunk in text.split("||") if chunk.strip()]


def test_chunk_backfill_detects_metadata_and_index_drift(tmp_path: Path):
    """dry-run 应识别 metadata 声称存在但索引本体缺失的 chunk 向量。"""
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "data" / "test.db"
    vector_dir = tmp_path / "vectors"
    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    markdown_store = MarkdownStore(vault_dir)
    sqlite_store = SQLiteStore(db_path)
    sqlite_store.initialize()
    embedder = DeterministicEmbedder()

    entry = Entry(
        title="Alpha Drift",
        source_type="generic",
        content="alpha first chunk||beta second chunk",
        summary_one_sentence="drift test",
    )
    file_path = markdown_store.save(entry)
    knowledge_id = sqlite_store.insert_entry(entry, str(file_path))
    sqlite_store.insert_chunks(knowledge_id, ["alpha first chunk", "beta second chunk"])

    vector_store = VectorStore(vector_dir, dim=embedder.dim)
    vector_store.add_chunk_vector(
        knowledge_id=knowledge_id,
        chunk_index=0,
        vector=embedder.embed_document("alpha first chunk"),
    )

    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["id_mapping"][
        str(VectorStore.encode_chunk_id(knowledge_id, 1))
    ] = [knowledge_id, 1]
    metadata_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    report = run_chunk_backfill(
        db_path=db_path,
        vector_index_dir=vector_dir,
        apply=False,
        embedding_dim=embedder.dim,
        embedder=embedder,
    )

    assert report.candidate_entries == 1
    assert report.candidates[0].knowledge_id == knowledge_id
    assert {"chunk_vector_mismatch", "irregular_chunk_rows"} & set(
        report.candidates[0].reasons
    )


def test_chunk_backfill_detects_stale_chunk_rows_even_when_indices_are_complete(
    tmp_path: Path,
):
    """dry-run 应识别当前内容切块结果与已落库 chunk 文本不一致的条目。"""
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "data" / "test.db"
    vector_dir = tmp_path / "vectors"
    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    markdown_store = MarkdownStore(vault_dir)
    sqlite_store = SQLiteStore(db_path)
    sqlite_store.initialize()
    embedder = DeterministicEmbedder()

    entry = Entry(
        title="Alpha Stale Chunks",
        source_type="generic",
        content="alpha current chunk||alpha fresh chunk",
        summary_one_sentence="stale rows test",
    )
    file_path = markdown_store.save(entry)
    knowledge_id = sqlite_store.insert_entry(entry, str(file_path))
    sqlite_store.insert_chunks(
        knowledge_id,
        ["beta stale chunk", "beta stale chunk"],
    )

    vector_store = VectorStore(vector_dir, dim=embedder.dim)
    vector_store.add_chunk_vectors(
        knowledge_id=knowledge_id,
        chunk_indices=[0, 1],
        vectors=np.vstack(
            [
                embedder.embed_document("beta stale chunk"),
                embedder.embed_document("beta stale chunk"),
            ]
        ).astype(np.float32),
    )

    report = run_chunk_backfill(
        db_path=db_path,
        vector_index_dir=vector_dir,
        apply=False,
        embedding_dim=embedder.dim,
        embedder=embedder,
    )

    assert report.candidate_entries == 1
    assert report.candidates[0].knowledge_id == knowledge_id
    assert "stale_chunk_rows" in report.candidates[0].reasons


def test_chunk_backfill_rewrites_orphan_vectors_when_chunk_rows_are_missing(
    tmp_path: Path,
):
    """apply 模式下补写 chunk 行时，也应同步重写遗留的旧 chunk 向量。"""
    vault_dir = tmp_path / "vault"
    db_path = tmp_path / "data" / "test.db"
    vector_dir = tmp_path / "vectors"
    vault_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    markdown_store = MarkdownStore(vault_dir)
    sqlite_store = SQLiteStore(db_path)
    sqlite_store.initialize()
    embedder = DeterministicEmbedder()

    entry = Entry(
        title="Alpha Rewrite",
        source_type="generic",
        content="alpha first chunk||alpha second chunk",
        summary_one_sentence="rewrite test",
    )
    file_path = markdown_store.save(entry)
    knowledge_id = sqlite_store.insert_entry(entry, str(file_path))

    vector_store = VectorStore(vector_dir, dim=embedder.dim)
    vector_store.add_chunk_vectors(
        knowledge_id=knowledge_id,
        chunk_indices=[0, 1],
        vectors=np.vstack(
            [
                embedder.embed_document("beta stale chunk"),
                embedder.embed_document("beta stale chunk"),
            ]
        ).astype(np.float32),
    )

    report = run_chunk_backfill(
        db_path=db_path,
        vector_index_dir=vector_dir,
        apply=True,
        embedding_dim=embedder.dim,
        embedder=embedder,
    )

    rewritten_store = VectorStore(vector_dir, dim=embedder.dim)
    chunk_label = VectorStore.encode_chunk_id(knowledge_id, 0)
    chunk_vector = rewritten_store.chunk_index.get_items([chunk_label])[0]
    chunk_rows = sqlite_store.get_chunks_by_knowledge_id(knowledge_id)

    assert report.candidate_entries == 1
    assert report.applied_entries == 1
    assert [row["chunk_text"] for row in chunk_rows] == [
        "alpha first chunk",
        "alpha second chunk",
    ]
    assert chunk_vector == embedder.embed_document("alpha first chunk").tolist()
