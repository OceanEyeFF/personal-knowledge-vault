from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from tests.offline_runtime import RUNTIME_PATH_ENV_KEYS, prepare_offline_child_env
from src.runtime.lifecycle import RuntimeReadiness, inspect_runtime
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_script(
    args: list[str],
    *,
    data_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if data_root is None:
        runtime = {key: os.environ[key] for key in RUNTIME_PATH_ENV_KEYS}
    else:
        runtime = {
            "DATA_DIR": str(data_root),
            "DB_PATH": str(data_root / "db" / "knowledge_vault.db"),
            "VAULT_DIR": str(data_root / "vault"),
            "VECTOR_DIR": str(data_root / "vectors"),
            "LOG_DIR": str(data_root / "logs"),
            "TMP_DIR": str(data_root / "tmp"),
        }
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides=runtime,
    )
    cmd = [
        sys.executable,
        "tests/offline_entrypoint.py",
        "python",
        "scripts/setup-test-db.py",
        *args,
    ]
    return subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
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


def test_setup_test_db_runtime_ready_publishes_matching_secret_free_fixture(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "runtime-ready"
    db_path = data_root / "db" / "knowledge_vault.db"
    result = _run_script(
        [
            "--seed",
            "123",
            "--count",
            "2",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--runtime-ready",
        ],
        data_root=data_root,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    runtime_snapshot_path = data_root / "config" / "local.yaml"
    snapshot = yaml.safe_load(runtime_snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 1
    assert snapshot["database"]["schema_version"]
    assert snapshot["embedding"]["provider"] == "openai_compatible"
    assert "api_key" not in runtime_snapshot_path.read_text(encoding="utf-8")
    assert (data_root / "tmp" / "jieba.cache").is_file()

    environment = {
        "PKV_TEST_OFFLINE": "1",
        "DATA_DIR": str(data_root),
        "DB_PATH": str(db_path),
        "VAULT_DIR": str(data_root / "vault"),
        "VECTOR_DIR": str(data_root / "vectors"),
        "LOG_DIR": str(data_root / "logs"),
        "TMP_DIR": str(data_root / "tmp"),
    }
    config = Config(
        str(PROJECT_ROOT / "config" / "config.yaml"),
        environment=environment,
        _user_config_updates={
            "ai.llm.api_key": "offline-test-placeholder",
            "ai.embedding.api_key": "offline-test-placeholder",
        },
    )
    assert inspect_runtime(config).readiness is RuntimeReadiness.READY


def test_setup_test_db_without_runtime_ready_does_not_publish_runtime_snapshot(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "ordinary-fixture"
    result = _run_script(
        [
            "--count",
            "1",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
        ],
        data_root=data_root,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (data_root / "config" / "local.yaml").exists()


def test_setup_test_db_runtime_ready_rejects_non_runtime_output(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime-ready-nondefault-output"
    other_output = data_root / "other" / "fixture.sqlite"
    result = _run_script(
        [
            "--count",
            "1",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--output",
            str(other_output),
            "--runtime-ready",
        ],
        data_root=data_root,
    )

    assert result.returncode == 1
    assert "--runtime-ready" in result.stdout
    assert not (data_root / "config" / "local.yaml").exists()


def test_setup_test_db_rejects_output_outside_selected_data_root() -> None:
    selected_data_root = Path(os.environ["DATA_DIR"])
    outside_root = selected_data_root.parent / (
        f"setup-db-sibling-reject-{uuid.uuid4().hex}"
    )
    db_path = outside_root / "forbidden" / "knowledge_vault.db"
    assert not outside_root.exists()

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
    assert not outside_root.exists()
    assert "Direct Python DATA_DIR" in result.stdout


def test_setup_test_db_rejects_sibling_data_test_output() -> None:
    db_path = (
        PROJECT_ROOT
        / ".data-test"
        / f"seed-sibling-{uuid.uuid4().hex[:12]}"
        / "db"
        / "knowledge_vault.db"
    )

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
    assert "Direct Python DATA_DIR" in result.stdout


def test_setup_test_db_data_root_named_db_keeps_derived_dirs_inside(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "db"
    db_path = data_root / "custom.sqlite"
    outside_vault = tmp_path / "vault"
    outside_vectors = tmp_path / "vectors"
    outside_vault.mkdir()
    outside_vectors.mkdir()
    vault_sentinel = outside_vault / "must-stay.txt"
    vector_sentinel = outside_vectors / "must-stay.txt"
    vault_sentinel.write_text("vault", encoding="utf-8")
    vector_sentinel.write_text("vectors", encoding="utf-8")

    result = _run_script(
        [
            "--seed",
            "42",
            "--count",
            "1",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--output",
            str(db_path),
        ],
        data_root=data_root,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert db_path.is_file()
    assert (data_root / "vault").is_dir()
    assert (data_root / "vectors").is_dir()
    assert vault_sentinel.read_text(encoding="utf-8") == "vault"
    assert vector_sentinel.read_text(encoding="utf-8") == "vectors"


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


def test_setup_test_db_removes_stale_sqlite_sidecars(tmp_path: Path) -> None:
    db_path = tmp_path / "sidecars" / "db" / "knowledge_vault.db"
    first = _run_script(
        [
            "--seed",
            "17",
            "--count",
            "2",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--output",
            str(db_path),
        ]
    )
    assert first.returncode == 0, first.stdout + first.stderr
    sidecars = [Path(f"{db_path}{suffix}") for suffix in ("-journal", "-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_text("stale", encoding="utf-8")

    second = _run_script(
        [
            "--seed",
            "17",
            "--count",
            "2",
            "--wechat-count",
            "0",
            "--zhihu-count",
            "0",
            "--output",
            str(db_path),
        ]
    )

    assert second.returncode == 0, second.stdout + second.stderr
    assert all(not sidecar.exists() for sidecar in sidecars)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] == 2


def test_setup_test_db_rejects_hardlinked_sqlite_sidecar(tmp_path: Path) -> None:
    db_path = tmp_path / "unsafe-sidecar" / "db" / "knowledge_vault.db"
    db_path.parent.mkdir(parents=True)
    source = tmp_path / "sidecar-source.txt"
    source.write_text("must-stay", encoding="utf-8")
    sidecar = Path(f"{db_path}-wal")
    try:
        os.link(source, sidecar)
    except OSError:
        pytest.skip("当前文件系统不支持硬链接")

    try:
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
        assert "链接或非普通文件" in result.stdout
        assert source.read_text(encoding="utf-8") == "must-stay"
        assert not db_path.exists()
    finally:
        sidecar.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
