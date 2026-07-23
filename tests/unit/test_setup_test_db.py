from __future__ import annotations

import sqlite3
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_script(args: list[str]) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "scripts/setup-test-db.py", *args]
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_setup_test_db_creates_database_and_records(tmp_path: Path) -> None:
    db_path = tmp_path / "db" / "knowledge_vault.db"
    result = _run_script(
        [
            "--seed",
            "123",
            "--count",
            "12",
            "--wechat-count",
            "3",
            "--zhihu-count",
            "2",
            "--output",
            str(db_path),
            "--allow-outside-test-root",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert db_path.exists()

    with sqlite3.connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
        assert total == 12

        wechat_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE source_type='wechat'"
        ).fetchone()[0]
        zhihu_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE source_type='zhihu'"
        ).fetchone()[0]
        text_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_items WHERE source_type='text'"
        ).fetchone()[0]

        assert wechat_count == 3
        assert zhihu_count == 2
        assert text_count == 7

        row = conn.execute(
            """
            SELECT title, keywords, tags, summary_100_words, content, file_path
            FROM knowledge_items
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    title, keywords, tags, summary_100_words, content, file_path = row
    assert title
    assert keywords
    assert tags
    assert summary_100_words
    assert content
    assert file_path
    assert Path(file_path).exists()
    assert Path(file_path).read_text(encoding="utf-8").strip()


def test_setup_test_db_rejects_external_output_without_explicit_opt_in(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "forbidden" / "knowledge_vault.db"

    result = _run_script(
        [
            "--count",
            "1",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--output",
            str(db_path),
        ]
    )

    assert result.returncode == 1
    assert not db_path.exists()
    assert "--allow-outside-test-root" in result.stdout


def test_setup_test_db_explicit_embedding_dim_creates_vector_artifacts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "vector-fixture" / "db" / "knowledge_vault.db"

    result = _run_script(
        [
            "--seed",
            "7",
            "--count",
            "2",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--embedding-dim",
            "8",
            "--output",
            str(db_path),
            "--allow-outside-test-root",
        ]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    vector_dir = db_path.parent.parent / "vectors"
    for name in ("doc_vectors", "chunk_vectors"):
        assert (vector_dir / f"{name}.idx").is_file()
        metadata_path = vector_dir / f"{name}_metadata.json"
        assert metadata_path.is_file()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["dim"] == 8
