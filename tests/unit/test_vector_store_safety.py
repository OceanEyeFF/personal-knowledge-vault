"""
VectorStore 安全性回归测试
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from src.storage.vector_store import VectorStore
from src.utils.config import endpoint_contract_sha256


def _fake_config(base_url: str, model: str, dim: int):
    return SimpleNamespace(
        embedding_dim=dim,
        embd_base_url=base_url,
        embd_model=model,
        embedding_index_fingerprint=lambda resolved_dim: {
            "base_url_sha256": endpoint_contract_sha256(base_url),
            "embedding_model": model,
            "embedding_dim": str(int(resolved_dim)),
        },
    )


def _legacy_fingerprint(endpoint: str, model: str = "model-a", dim: int = 4):
    return {
        "base_url": endpoint,
        "embedding_model": model,
        "embedding_dim": str(dim),
    }


def _replace_all_fingerprints(vector_dir: Path, fingerprints: dict[str, dict]):
    for name, fingerprint in fingerprints.items():
        metadata_path = vector_dir / f"{name}_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("schema_version", None)
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
        metadata["embedding_fingerprint"] = fingerprint
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")


def test_vector_store_dimension_mismatch_does_not_rebuild_existing_index(
    tmp_path: Path,
):
    """维度不匹配时不应删除既有索引数据。"""
    vector_dir = tmp_path / "vectors"
    original_store = VectorStore(vector_dir, dim=4)
    original_store.add_doc_vector(knowledge_id=1, vector=np.ones(4, dtype=np.float32))
    original_store.add_chunk_vector(
        knowledge_id=1,
        chunk_index=0,
        vector=np.arange(4, dtype=np.float32),
    )

    with pytest.raises(RuntimeError, match="索引维度不匹配"):
        VectorStore(vector_dir, dim=8)

    recovered_store = VectorStore(vector_dir, dim=4)
    stats = recovered_store.get_index_stats()

    assert stats["doc_count"] == 1
    assert stats["chunk_count"] == 1
    assert recovered_store.get_chunk_indices_for_entry(1) == [0]


def test_dimension_mismatch_still_scrubs_both_legacy_fingerprints(
    tmp_path: Path,
):
    """维度校验报错前也必须先清理 doc/chunk 两份 endpoint 明文。"""
    vector_dir = tmp_path / "vectors"
    endpoint = (
        "https://dim-user:dim-password@embd.example.com/v1"
        "?api_key=dim-query"
    )
    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(endpoint, "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    _replace_all_fingerprints(
        vector_dir,
        {
            "doc_vectors": _legacy_fingerprint(endpoint),
            "chunk_vectors": _legacy_fingerprint(endpoint),
        },
    )

    with pytest.raises(RuntimeError, match="索引维度不匹配"):
        VectorStore(vector_dir, dim=8)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        assert "dim-user" not in persisted
        assert "dim-password" not in persisted
        assert "dim-query" not in persisted
        assert "base_url_sha256" in persisted


def test_vector_store_defaults_to_existing_index_dimension(tmp_path: Path):
    """未显式传入维度时应沿用已有索引维度。"""
    vector_dir = tmp_path / "vectors"
    original_store = VectorStore(vector_dir, dim=4)
    original_store.add_doc_vector(knowledge_id=7, vector=np.ones(4, dtype=np.float32))

    reopened_store = VectorStore(vector_dir)

    assert reopened_store.dim == 4
    assert reopened_store.get_index_stats()["doc_count"] == 1


def test_vector_store_persists_embedding_fingerprint(tmp_path: Path):
    """新索引元数据应记录非敏感 Embedding 契约指纹。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        store = VectorStore(vector_dir, dim=4)

    metadata = json.loads((vector_dir / "doc_vectors_metadata.json").read_text())

    assert metadata["schema_version"] == VectorStore.METADATA_SCHEMA_VERSION
    assert metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY] == {
        "schema_version": VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION,
        "base_url_sha256": endpoint_contract_sha256(
            "https://embd.example.com/v1"
        ),
        "embedding_model": "model-a",
        "embedding_dim": "4",
    }
    assert metadata["embedding_fingerprint"] == {
        "base_url": "https://embd.example.com/v1",
        "embedding_model": "model-a",
        "embedding_dim": "4",
    }
    assert store.get_index_stats()["embedding_fingerprint"]["embedding_model"] == "model-a"


def test_vector_store_rejects_same_dim_embedding_model_drift(tmp_path: Path):
    """同维度换 Embedding 模型也不能复用旧索引。"""
    vector_dir = tmp_path / "vectors"

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-b", 4),
    ):
        with pytest.raises(RuntimeError, match="Embedding 索引契约不匹配"):
            VectorStore(vector_dir, dim=4)


def test_vector_store_rejects_same_model_embedding_endpoint_drift(tmp_path: Path):
    """同模型同维度但切换 Embedding 端点也不能静默复用旧索引。"""
    vector_dir = tmp_path / "vectors"

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd-a.example.com/v1", "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd-b.example.com/v1", "model-a", 4),
    ):
        with pytest.raises(RuntimeError, match="Embedding 索引契约不匹配"):
            VectorStore(vector_dir, dim=4)


def test_vector_store_migrates_matching_plaintext_endpoint_fingerprint(
    tmp_path: Path,
):
    """旧元数据的明文 endpoint 在同契约下应脱敏迁移并兼容加载。"""
    vector_dir = tmp_path / "vectors"
    original_endpoint = (
        "https://old-user:old-password@embd.example.com/v1"
        "?api_key=old-query&region=cn"
    )
    rotated_endpoint = (
        "https://new-user:new-password@embd.example.com/v1"
        "?api_key=new-query&region=cn"
    )

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(original_endpoint, "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    _replace_all_fingerprints(
        vector_dir,
        {
            "doc_vectors": _legacy_fingerprint(original_endpoint),
            "chunk_vectors": _legacy_fingerprint(original_endpoint),
        },
    )

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(rotated_endpoint, "model-a", 4),
    ):
        reopened = VectorStore(vector_dir, dim=4)

    assert reopened.dim == 4
    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(persisted)
        assert "old-user" not in persisted
        assert "old-password" not in persisted
        assert "old-query" not in persisted
        assert metadata["schema_version"] == VectorStore.METADATA_SCHEMA_VERSION
        assert "embedding_fingerprint" not in metadata
        assert metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY] == {
            "schema_version": VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION,
            "base_url_sha256": endpoint_contract_sha256(rotated_endpoint),
            "embedding_model": "model-a",
            "embedding_dim": "4",
        }


def test_safe_legacy_fingerprint_keeps_82381bb_rollback_fields(tmp_path: Path):
    """无凭据旧 endpoint 迁移后仍可供 82381bb reader 读取三项旧字段。"""
    vector_dir = tmp_path / "vectors"
    endpoint = "https://embd.example.com/v1?region=cn"
    config = _fake_config(endpoint, "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    _replace_all_fingerprints(
        vector_dir,
        {
            "doc_vectors": _legacy_fingerprint(endpoint),
            "chunk_vectors": _legacy_fingerprint(endpoint),
        },
    )

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        fingerprint = metadata["embedding_fingerprint"]
        fingerprint_v2 = metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY]
        legacy_reader_view = {
            key: str(fingerprint.get(key, ""))
            for key in ("base_url", "embedding_model", "embedding_dim")
        }

        assert metadata["schema_version"] == VectorStore.METADATA_SCHEMA_VERSION
        assert fingerprint_v2["schema_version"] == (
            VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION
        )
        assert fingerprint_v2["base_url_sha256"] == endpoint_contract_sha256(
            endpoint
        )
        assert legacy_reader_view == {
            "base_url": endpoint,
            "embedding_model": "model-a",
            "embedding_dim": "4",
        }


def test_credential_endpoint_never_persists_legacy_base_url(tmp_path: Path):
    """endpoint 含凭据时安全优先，不写 82381bb 所需的 base_url。"""
    vector_dir = tmp_path / "vectors"
    endpoint = (
        "https://credential-user:credential-password@embd.example.com/v1"
        "?api_key=credential-query"
    )

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(endpoint, "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(persisted)
        fingerprint = metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY]

        assert metadata["schema_version"] == VectorStore.METADATA_SCHEMA_VERSION
        assert fingerprint["schema_version"] == (
            VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION
        )
        assert fingerprint["base_url_sha256"] == endpoint_contract_sha256(endpoint)
        assert "embedding_fingerprint" not in metadata
        assert "base_url" not in fingerprint
        assert "credential-user" not in persisted
        assert "credential-password" not in persisted
        assert "credential-query" not in persisted


def test_switching_to_credential_endpoint_removes_both_legacy_keys(
    tmp_path: Path,
):
    """已存在的安全 legacy 键也须在 credential 契约校验前从两份文件移除。"""
    vector_dir = tmp_path / "vectors"
    safe_endpoint = "https://embd.example.com/v1?region=cn"
    credential_endpoint = (
        "https://switch-user:switch-password@embd.example.com/v1?region=cn"
    )

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(safe_endpoint, "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        assert "embedding_fingerprint" in json.loads(
            metadata_path.read_text(encoding="utf-8")
        )

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(credential_endpoint, "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(persisted)
        assert "embedding_fingerprint" not in metadata
        assert VectorStore.EMBEDDING_FINGERPRINT_V2_KEY in metadata
        assert "switch-user" not in persisted
        assert "switch-password" not in persisted


@pytest.mark.parametrize("drifted_name", ["doc_vectors", "chunk_vectors"])
def test_fingerprint_drift_still_scrubs_both_metadata_files(
    tmp_path: Path,
    drifted_name: str,
):
    """任一索引契约 drift 报错前，doc/chunk 两份明文都必须先脱敏。"""
    vector_dir = tmp_path / "vectors"
    current_endpoint = (
        "https://current-user:current-password@embd.example.com/v1"
        "?api_key=current-query&region=cn"
    )
    drifted_endpoint = (
        "https://drift-user:drift-password@other.example.com/v1"
        "?api_key=drift-query&region=cn"
    )
    config = _fake_config(current_endpoint, "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    fingerprints = {
        "doc_vectors": _legacy_fingerprint(current_endpoint),
        "chunk_vectors": _legacy_fingerprint(current_endpoint),
    }
    fingerprints[drifted_name] = _legacy_fingerprint(drifted_endpoint)
    _replace_all_fingerprints(vector_dir, fingerprints)

    with patch("src.storage.vector_store.get_config", return_value=config):
        with pytest.raises(RuntimeError, match="Embedding 索引契约不匹配"):
            VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(persisted)
        fingerprint = metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY]
        assert "embedding_fingerprint" not in metadata
        assert "base_url" not in fingerprint
        assert fingerprint["schema_version"] == (
            VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION
        )
        for secret in (
            "current-user",
            "current-password",
            "current-query",
            "drift-user",
            "drift-password",
            "drift-query",
        ):
            assert secret not in persisted


def test_vector_store_mismatch_does_not_echo_legacy_endpoint_credentials(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """旧 endpoint 指纹不匹配时，异常也只显示哈希值。"""
    vector_dir = tmp_path / "vectors"
    current_endpoint = "https://current.example.com/v1"
    legacy_endpoint = (
        "https://error-user:error-password@other.example.com/v1"
        "?code=error-code"
    )
    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(current_endpoint, "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    _replace_all_fingerprints(
        vector_dir,
        {
            "doc_vectors": _legacy_fingerprint(legacy_endpoint),
            "chunk_vectors": _legacy_fingerprint(legacy_endpoint),
        },
    )

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config(current_endpoint, "model-a", 4),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            VectorStore(vector_dir, dim=4)

    message = str(exc_info.value)
    assert "Embedding 索引契约不匹配" in message
    assert "error-user" not in message
    assert "error-password" not in message
    assert "error-code" not in message
    assert "error-user" not in caplog.text
    assert "error-password" not in caplog.text
    assert "error-code" not in caplog.text
    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        assert "error-user" not in persisted
        assert "error-password" not in persisted
        assert "error-code" not in persisted
        assert "base_url_sha256" in persisted


@pytest.mark.parametrize(
    "failure_target",
    (
        "src.storage.vector_store.json.dump",
        "src.storage.vector_store.os.fsync",
        "src.storage.vector_store.os.replace",
    ),
    ids=("dump", "fsync", "replace"),
)
def test_atomic_legacy_migration_failure_preserves_original_bytes(
    tmp_path: Path,
    failure_target: str,
):
    """dump/fsync/replace 任一步失败都不能截断原文件，并须清理临时文件。"""
    vector_dir = tmp_path / "vectors"
    vector_dir.mkdir()
    metadata_path = vector_dir / "doc_vectors_metadata.json"
    endpoint = (
        "https://atomic-user:atomic-password@embd.example.com/v1"
        "?api_key=atomic-query"
    )
    original_bytes = (
        json.dumps(
            {
                "dim": 4,
                "embedding_fingerprint": _legacy_fingerprint(endpoint),
                "id_mapping": {},
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    metadata_path.write_bytes(original_bytes)

    with patch(failure_target, side_effect=OSError("injected migration failure")):
        with pytest.raises(RuntimeError, match="Embedding 元数据安全迁移失败") as exc_info:
            VectorStore(vector_dir, dim=4)

    assert metadata_path.read_bytes() == original_bytes
    assert list(vector_dir.glob(f".{metadata_path.name}.*.tmp")) == []
    assert "atomic-user" not in str(exc_info.value)
    assert "atomic-password" not in str(exc_info.value)
    assert "atomic-query" not in str(exc_info.value)


def test_one_file_migration_failure_does_not_block_other_file(tmp_path: Path):
    """doc 原子替换失败时仍继续完成 chunk 的安全迁移。"""
    vector_dir = tmp_path / "vectors"
    vector_dir.mkdir()
    endpoint = (
        "https://partial-user:partial-password@embd.example.com/v1"
        "?api_key=partial-query"
    )
    original_by_name: dict[str, bytes] = {}
    for name in ("doc_vectors", "chunk_vectors"):
        metadata_path = vector_dir / f"{name}_metadata.json"
        original = json.dumps(
            {
                "dim": 4,
                "embedding_fingerprint": _legacy_fingerprint(endpoint),
                "id_mapping": {},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        metadata_path.write_bytes(original)
        original_by_name[name] = original

    real_replace = os.replace

    def fail_doc_replace(source: Path, destination: Path):
        if Path(destination).name == "doc_vectors_metadata.json":
            raise OSError("injected doc replace failure")
        return real_replace(source, destination)

    with patch("src.storage.vector_store.os.replace", side_effect=fail_doc_replace):
        with pytest.raises(RuntimeError, match="doc_vectors") as exc_info:
            VectorStore(vector_dir, dim=4)

    doc_path = vector_dir / "doc_vectors_metadata.json"
    chunk_path = vector_dir / "chunk_vectors_metadata.json"
    chunk_text = chunk_path.read_text(encoding="utf-8")
    assert doc_path.read_bytes() == original_by_name["doc_vectors"]
    assert chunk_path.read_bytes() != original_by_name["chunk_vectors"]
    chunk_metadata = json.loads(chunk_text)
    assert "embedding_fingerprint" not in chunk_metadata
    assert "base_url" not in chunk_metadata[
        VectorStore.EMBEDDING_FINGERPRINT_V2_KEY
    ]
    assert "partial-user" not in chunk_text
    assert "partial-password" not in chunk_text
    assert "partial-query" not in chunk_text
    assert "partial-user" not in str(exc_info.value)
    assert list(vector_dir.glob(".*_metadata.json.*.tmp")) == []


def test_metadata_migration_is_idempotent(tmp_path: Path):
    """完成一次 schema 迁移后，再次初始化不得重复改写 metadata。"""
    vector_dir = tmp_path / "vectors"
    endpoint = "https://embd.example.com/v1?region=cn"
    config = _fake_config(endpoint, "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    _replace_all_fingerprints(
        vector_dir,
        {
            "doc_vectors": _legacy_fingerprint(endpoint),
            "chunk_vectors": _legacy_fingerprint(endpoint),
        },
    )
    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    first_pass = {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    }
    with (
        patch("src.storage.vector_store.get_config", return_value=config),
        patch.object(VectorStore, "_atomic_write_json") as atomic_write,
    ):
        VectorStore(vector_dir, dim=4)

    atomic_write.assert_not_called()
    assert {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    } == first_pass


@pytest.mark.parametrize(
    ("endpoint", "keeps_legacy"),
    (
        ("https://embd.example.com/v1?region=cn", True),
        (
            "https://hash-user:hash-password@embd.example.com/v1"
            "?api_key=hash-query",
            False,
        ),
    ),
)
def test_hash_in_v1_transition_format_migrates_to_official_v2(
    tmp_path: Path,
    endpoint: str,
    keeps_legacy: bool,
):
    """兼容第一轮可能落盘的 hash-in-v1 过渡格式。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config(endpoint, "model-a", 4)
    transition_fingerprint = {
        "schema_version": VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION,
        "base_url_sha256": endpoint_contract_sha256(endpoint),
        "embedding_model": "model-a",
        "embedding_dim": "4",
    }

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
        metadata["embedding_fingerprint"] = transition_fingerprint
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        persisted = metadata_path.read_text(encoding="utf-8")
        metadata = json.loads(persisted)
        assert metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY] == (
            transition_fingerprint
        )
        assert ("embedding_fingerprint" in metadata) is keeps_legacy
        if keeps_legacy:
            assert metadata["embedding_fingerprint"] == _legacy_fingerprint(endpoint)
        else:
            assert "hash-user" not in persisted
            assert "hash-password" not in persisted
            assert "hash-query" not in persisted


@pytest.mark.parametrize("legacy_name", ["doc_vectors", "chunk_vectors"])
def test_mixed_raw_v1_and_v2_metadata_migrates_idempotently(
    tmp_path: Path,
    legacy_name: str,
):
    """doc/chunk 任意 v1/v2 混合状态都可原子续迁。"""
    vector_dir = tmp_path / "vectors"
    endpoint = "https://embd.example.com/v1?region=cn"
    config = _fake_config(endpoint, "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    for name in ("doc_vectors", "chunk_vectors"):
        metadata_path = vector_dir / f"{name}_metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if name == legacy_name:
            metadata.pop("schema_version", None)
            metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
            metadata["embedding_fingerprint"] = _legacy_fingerprint(endpoint)
        else:
            metadata.pop("embedding_fingerprint", None)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    first_pass = {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    }
    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert VectorStore.EMBEDDING_FINGERPRINT_V2_KEY in metadata
        assert metadata["embedding_fingerprint"] == _legacy_fingerprint(endpoint)
    assert {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    } == first_pass


@pytest.mark.parametrize("failed_metadata_number", [1, 2])
def test_creation_metadata_dump_failure_is_recoverable(
    tmp_path: Path,
    failed_metadata_number: int,
):
    """doc/chunk 任一步 metadata 失败都回收对应 idx，下一次可正常重试。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    real_dump = json.dump
    dump_count = 0

    def fail_selected_dump(payload, file, **kwargs):
        nonlocal dump_count
        dump_count += 1
        if dump_count == failed_metadata_number:
            raise OSError("injected runtime dump failure")
        return real_dump(payload, file, **kwargs)

    with (
        patch("src.storage.vector_store.get_config", return_value=config),
        patch(
            "src.storage.vector_store.json.dump",
            side_effect=fail_selected_dump,
        ),
    ):
        with pytest.raises(OSError, match="runtime dump failure"):
            VectorStore(vector_dir, dim=4)

    failed_name = "doc_vectors" if failed_metadata_number == 1 else "chunk_vectors"
    metadata_path = vector_dir / f"{failed_name}_metadata.json"
    assert not metadata_path.exists()
    assert not (vector_dir / f"{failed_name}.idx").exists()
    assert list(vector_dir.glob(f".{metadata_path.name}.*.tmp")) == []
    if failed_metadata_number == 2:
        assert (vector_dir / "doc_vectors.idx").exists()
        assert (vector_dir / "doc_vectors_metadata.json").exists()

    with patch("src.storage.vector_store.get_config", return_value=config):
        recovered = VectorStore(vector_dir, dim=4)

    assert recovered.dim == 4
    for name in ("doc_vectors", "chunk_vectors"):
        assert (vector_dir / f"{name}.idx").exists()
        assert (vector_dir / f"{name}_metadata.json").exists()


@pytest.mark.parametrize("writer_name", ["single", "batch"])
def test_runtime_mapping_dump_failure_preserves_metadata_bytes(
    tmp_path: Path,
    writer_name: str,
):
    """single/batch mapping 写失败均不得截断 metadata。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        store = VectorStore(vector_dir, dim=4)

    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    original_bytes = metadata_path.read_bytes()
    with patch(
        "src.storage.vector_store.json.dump",
        side_effect=OSError("injected runtime dump failure"),
    ):
        with pytest.raises(OSError, match="runtime dump failure"):
            if writer_name == "single":
                store._update_metadata("chunk_vectors", 10000, (1, 0))
            else:
                store._update_metadata_batch(
                    "chunk_vectors",
                    {10000: (1, 0), 10001: (1, 1)},
                )

    assert metadata_path.read_bytes() == original_bytes
    assert list(vector_dir.glob(f".{metadata_path.name}.*.tmp")) == []


@pytest.mark.parametrize(
    "delete_method",
    ["delete_vectors_for_entry", "delete_chunk_vectors_for_entry"],
)
def test_runtime_delete_dump_failure_preserves_metadata_bytes(
    tmp_path: Path,
    delete_method: str,
):
    """两条 delete metadata 写路径失败时都保留原字节。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        store = VectorStore(vector_dir, dim=4)
    store.add_doc_vector(1, np.ones(4, dtype=np.float32))
    store.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))

    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    original_bytes = metadata_path.read_bytes()
    with patch(
        "src.storage.vector_store.json.dump",
        side_effect=OSError("injected delete dump failure"),
    ):
        with pytest.raises(OSError, match="delete dump failure"):
            getattr(store, delete_method)(1)

    assert metadata_path.read_bytes() == original_bytes
    assert list(vector_dir.glob(f".{metadata_path.name}.*.tmp")) == []


@pytest.mark.parametrize("writer_name", ["doc", "single_chunk", "batch_chunk"])
def test_runtime_add_save_failure_restores_index_metadata_pair(
    tmp_path: Path,
    writer_name: str,
):
    """add/batch 的 idx 保存失败后必须恢复配对原字节并可重试。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        store = VectorStore(vector_dir, dim=4)

    pair_name = "doc_vectors" if writer_name == "doc" else "chunk_vectors"
    index_path = vector_dir / f"{pair_name}.idx"
    metadata_path = vector_dir / f"{pair_name}_metadata.json"
    original_index_bytes = index_path.read_bytes()
    original_metadata_bytes = metadata_path.read_bytes()

    def corrupt_target_then_fail(name: str) -> None:
        assert name == pair_name
        index_path.write_bytes(b"injected partial index")
        raise OSError("injected add index save failure")

    with patch.object(store, "_save_index", side_effect=corrupt_target_then_fail):
        with pytest.raises(OSError, match="add index save failure"):
            if writer_name == "doc":
                store.add_doc_vector(1, np.ones(4, dtype=np.float32))
            elif writer_name == "single_chunk":
                store.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))
            else:
                store.add_chunk_vectors(
                    1,
                    [0, 1],
                    np.ones((2, 4), dtype=np.float32),
                )

    assert index_path.read_bytes() == original_index_bytes
    assert metadata_path.read_bytes() == original_metadata_bytes
    assert list(vector_dir.glob(".*.rollback")) == []
    assert list(vector_dir.glob(".*.tmp")) == []

    if writer_name == "doc":
        assert store.get_doc_vector(1) is None
        store.add_doc_vector(1, np.ones(4, dtype=np.float32))
    elif writer_name == "single_chunk":
        assert store.get_chunk_indices_for_entry(1) == []
        store.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))
    else:
        assert store.get_chunk_indices_for_entry(1) == []
        store.add_chunk_vectors(
            1,
            [0, 1],
            np.ones((2, 4), dtype=np.float32),
        )

    with patch("src.storage.vector_store.get_config", return_value=config):
        reopened = VectorStore(vector_dir, dim=4)
    if writer_name == "doc":
        assert reopened.get_doc_vector(1) is not None
    elif writer_name == "single_chunk":
        assert reopened.get_chunk_indices_for_entry(1) == [0]
    else:
        assert reopened.get_chunk_indices_for_entry(1) == [0, 1]


@pytest.mark.parametrize(
    "delete_method",
    ["delete_vectors_for_entry", "delete_chunk_vectors_for_entry"],
)
def test_runtime_delete_save_failure_restores_chunk_pair_and_retry_succeeds(
    tmp_path: Path,
    delete_method: str,
):
    """两条 delete 在 idx 保存失败后均保留 mapping，且同实例重试可成功。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        store = VectorStore(vector_dir, dim=4)
    store.add_doc_vector(1, np.ones(4, dtype=np.float32))
    store.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))

    index_path = vector_dir / "chunk_vectors.idx"
    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    original_index_bytes = index_path.read_bytes()
    original_metadata_bytes = metadata_path.read_bytes()
    real_save_index = store._save_index

    def fail_chunk_save(name: str) -> None:
        if name == "chunk_vectors":
            index_path.write_bytes(b"injected partial delete index")
            raise OSError("injected delete index save failure")
        real_save_index(name)

    with patch.object(store, "_save_index", side_effect=fail_chunk_save):
        with pytest.raises(OSError, match="delete index save failure"):
            getattr(store, delete_method)(1)

    assert index_path.read_bytes() == original_index_bytes
    assert metadata_path.read_bytes() == original_metadata_bytes
    assert store.search_chunk(np.ones(4, dtype=np.float32), k=1)[0][:2] == (1, 0)
    assert store.get_chunk_indices_for_entry(1) == [0]
    assert list(vector_dir.glob(".*.rollback")) == []
    assert list(vector_dir.glob(".*.tmp")) == []

    retry_result = getattr(store, delete_method)(1)
    if delete_method == "delete_vectors_for_entry":
        assert retry_result["chunks_deleted"] == 1
    else:
        assert retry_result == 1
    assert store.search_chunk(np.ones(4, dtype=np.float32), k=10) == []

    with patch("src.storage.vector_store.get_config", return_value=config):
        reopened = VectorStore(vector_dir, dim=4)
    assert reopened.get_chunk_indices_for_entry(1) == []
    assert reopened.search_chunk(np.ones(4, dtype=np.float32), k=10) == []


def test_delete_vectors_doc_save_failure_restores_pair_and_retry_succeeds(
    tmp_path: Path,
):
    """组合 delete 的 doc 保存点失败时也恢复旧 idx，并允许同实例重试。"""
    vector_dir = tmp_path / "vectors"
    store = VectorStore(vector_dir, dim=4)
    store.add_doc_vector(1, np.ones(4, dtype=np.float32))
    store.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))

    index_path = vector_dir / "doc_vectors.idx"
    metadata_path = vector_dir / "doc_vectors_metadata.json"
    original_index_bytes = index_path.read_bytes()
    original_metadata_bytes = metadata_path.read_bytes()

    def corrupt_doc_then_fail(name: str) -> None:
        assert name == "doc_vectors"
        index_path.write_bytes(b"injected partial doc delete index")
        raise OSError("injected doc delete index save failure")

    with patch.object(store, "_save_index", side_effect=corrupt_doc_then_fail):
        with pytest.raises(OSError, match="doc delete index save failure"):
            store.delete_vectors_for_entry(1)

    assert index_path.read_bytes() == original_index_bytes
    assert metadata_path.read_bytes() == original_metadata_bytes
    assert store.get_doc_vector(1) is not None
    assert store.get_chunk_indices_for_entry(1) == [0]
    assert list(vector_dir.glob(".*.rollback")) == []
    assert list(vector_dir.glob(".*.tmp")) == []

    retry_result = store.delete_vectors_for_entry(1)
    assert retry_result == {"doc_deleted": True, "chunks_deleted": 1}
    assert store.search_doc(np.ones(4, dtype=np.float32), k=10) == []
    assert store.search_chunk(np.ones(4, dtype=np.float32), k=10) == []
    reopened = VectorStore(vector_dir, dim=4)
    assert reopened.get_doc_vector(1) is None
    assert reopened.get_chunk_indices_for_entry(1) == []
    assert reopened.search_doc(np.ones(4, dtype=np.float32), k=10) == []
    assert reopened.search_chunk(np.ones(4, dtype=np.float32), k=10) == []


def test_search_doc_uses_active_count_after_partial_and_full_delete(tmp_path: Path):
    """doc 搜索在 active<k 和 active=0 时分别返回剩余项与空列表。"""
    store = VectorStore(tmp_path / "vectors", dim=4)
    first_vector = np.array([1, 0, 0, 0], dtype=np.float32)
    second_vector = np.array([0, 1, 0, 0], dtype=np.float32)
    store.add_doc_vector(1, first_vector)
    store.add_doc_vector(2, second_vector)

    assert store.delete_vectors_for_entry(1)["doc_deleted"] is True
    assert store.doc_index.get_current_count() == 2
    partial_results = store.search_doc(first_vector, k=10)
    assert [knowledge_id for knowledge_id, _ in partial_results] == [2]

    assert store.delete_vectors_for_entry(2)["doc_deleted"] is True
    assert store.doc_index.get_current_count() == 2
    assert store.search_doc(first_vector, k=10) == []


def test_search_chunk_uses_active_mapping_after_partial_and_full_delete(
    tmp_path: Path,
):
    """chunk 搜索只返回仍有 mapping 的 active 项，全部删除后返回空列表。"""
    store = VectorStore(tmp_path / "vectors", dim=4)
    first_vector = np.array([1, 0, 0, 0], dtype=np.float32)
    second_vector = np.array([0, 1, 0, 0], dtype=np.float32)
    store.add_chunk_vector(1, 0, first_vector)
    store.add_chunk_vector(2, 0, second_vector)

    assert store.delete_chunk_vectors_for_entry(1) == 1
    assert store.chunk_index.get_current_count() == 2
    partial_results = store.search_chunk(first_vector, k=10)
    assert [(knowledge_id, chunk_index) for knowledge_id, chunk_index, _ in partial_results] == [
        (2, 0)
    ]

    assert store.delete_chunk_vectors_for_entry(2) == 1
    assert store.chunk_index.get_current_count() == 2
    assert store.search_chunk(first_vector, k=10) == []


def test_stale_instance_chunk_search_filters_latest_active_mapping(tmp_path: Path):
    """旧实例应跳过另一实例已删除的最近 chunk，并返回较远 active mapping。"""
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=4)
    first_vector = np.array([1, 0, 0, 0], dtype=np.float32)
    second_vector = np.array([0, 1, 0, 0], dtype=np.float32)
    writer.add_chunk_vector(1, 0, first_vector)
    writer.add_chunk_vector(2, 0, second_vector)
    stale_reader = VectorStore(vector_dir, dim=4)

    assert writer.delete_chunk_vectors_for_entry(1) == 1
    assert stale_reader.chunk_index.get_current_count() == 2
    results = stale_reader.search_chunk(first_vector, k=1)

    assert [(knowledge_id, chunk_index) for knowledge_id, chunk_index, _ in results] == [
        (2, 0)
    ]


def test_search_chunk_skips_non_finite_mapping_values(tmp_path: Path):
    """畸形 Infinity mapping 不应触发 int() OverflowError 使搜索崩溃。"""
    vector_dir = tmp_path / "vectors"
    store = VectorStore(vector_dir, dim=4)
    store.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))
    metadata_path = vector_dir / "chunk_vectors_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["id_mapping"]["10000"] = [float("inf"), 0]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    assert store.search_chunk(np.ones(4, dtype=np.float32), k=1) == []


def test_stale_reader_search_sees_latest_doc_and_chunk_adds_and_deletes(
    tmp_path: Path,
):
    """双实例 reader 每次搜索都应看到磁盘最新 doc/chunk 配对。"""
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=4)
    stale_reader = VectorStore(vector_dir, dim=4)
    first_vector = np.array([1, 0, 0, 0], dtype=np.float32)
    second_vector = np.array([0, 1, 0, 0], dtype=np.float32)

    writer.add_doc_vector(1, first_vector)
    writer.add_chunk_vector(1, 0, first_vector)
    assert [item[0] for item in stale_reader.search_doc(first_vector, k=10)] == [1]
    assert [item[:2] for item in stale_reader.search_chunk(first_vector, k=10)] == [
        (1, 0)
    ]

    writer.add_doc_vector(2, second_vector)
    writer.add_chunk_vector(2, 0, second_vector)
    assert {item[0] for item in stale_reader.search_doc(first_vector, k=10)} == {
        1,
        2,
    }
    assert {
        item[:2] for item in stale_reader.search_chunk(first_vector, k=10)
    } == {(1, 0), (2, 0)}

    assert writer.delete_vectors_for_entry(1) == {
        "doc_deleted": True,
        "chunks_deleted": 1,
    }
    assert [item[0] for item in stale_reader.search_doc(first_vector, k=10)] == [2]
    assert [item[:2] for item in stale_reader.search_chunk(first_vector, k=10)] == [
        (2, 0)
    ]


def test_concurrent_search_and_pair_writes_do_not_crash(tmp_path: Path):
    """双实例并发 add/delete/search 应由 pair lock 串行化且最终状态可见。"""
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=4)
    reader = VectorStore(vector_dir, dim=4)
    start = threading.Barrier(2)
    query = np.ones(4, dtype=np.float32)

    def write_vectors() -> None:
        start.wait()
        for knowledge_id in range(1, 9):
            vector = np.full(4, knowledge_id, dtype=np.float32)
            writer.add_doc_vector(knowledge_id, vector)
            writer.add_chunk_vector(knowledge_id, 0, vector)
            if knowledge_id > 1:
                writer.delete_vectors_for_entry(knowledge_id - 1)

    def search_vectors() -> None:
        start.wait()
        for _ in range(24):
            reader.search_doc(query, k=10)
            reader.search_chunk(query, k=10)

    with ThreadPoolExecutor(max_workers=2) as executor:
        write_future = executor.submit(write_vectors)
        search_future = executor.submit(search_vectors)
        write_future.result()
        search_future.result()

    assert [item[0] for item in reader.search_doc(query, k=10)] == [8]
    assert [item[:2] for item in reader.search_chunk(query, k=10)] == [(8, 0)]


def test_stale_reader_get_doc_vector_sees_latest_add_and_delete(tmp_path: Path):
    """get_doc_vector 应在双实例下即时看到另一实例的 add/delete。"""
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=4)
    stale_reader = VectorStore(vector_dir, dim=4)
    vector = np.array([1, 0, 0, 0], dtype=np.float32)

    assert stale_reader.get_doc_vector(1) is None
    writer.add_doc_vector(1, vector)
    np.testing.assert_allclose(stale_reader.get_doc_vector(1), vector)
    assert stale_reader.get_index_stats()["doc_count"] == 1

    assert writer.delete_vectors_for_entry(1)["doc_deleted"] is True
    assert stale_reader.get_doc_vector(1) is None


def test_concurrent_get_doc_vector_and_pair_writes_do_not_crash(tmp_path: Path):
    """双实例并发 get/add/delete 应串行化，并在结束后返回最新 doc 状态。"""
    vector_dir = tmp_path / "vectors"
    writer = VectorStore(vector_dir, dim=4)
    reader = VectorStore(vector_dir, dim=4)
    start = threading.Barrier(2)

    def write_vectors() -> None:
        start.wait()
        for knowledge_id in range(1, 9):
            vector = np.zeros(4, dtype=np.float32)
            vector[(knowledge_id - 1) % 4] = 1
            writer.add_doc_vector(knowledge_id, vector)
            if knowledge_id > 1:
                writer.delete_vectors_for_entry(knowledge_id - 1)

    def get_vectors() -> None:
        start.wait()
        for operation in range(48):
            reader.get_doc_vector(operation % 8 + 1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        write_future = executor.submit(write_vectors)
        read_future = executor.submit(get_vectors)
        write_future.result()
        read_future.result()

    assert reader.get_doc_vector(7) is None
    assert reader.get_doc_vector(8) is not None


def test_get_doc_vector_only_swallows_missing_label_runtime_error(tmp_path: Path):
    """非 Label-not-found 的 hnsw RuntimeError 必须继续上抛。"""
    store = VectorStore(tmp_path / "vectors", dim=4)

    class BrokenIndex:
        @staticmethod
        def get_items(_labels):
            raise RuntimeError("injected corrupt index failure")

    with patch.object(
        store,
        "_reload_index_for_update_locked",
        return_value=BrokenIndex(),
    ):
        with pytest.raises(RuntimeError, match="corrupt index failure"):
            store.get_doc_vector(1)


def test_atomic_index_save_partial_temp_failure_preserves_target(tmp_path: Path):
    """hnswlib 写临时 idx 后失败时，最终 idx 原字节不变且临时文件被清理。"""
    vector_dir = tmp_path / "vectors"
    store = VectorStore(vector_dir, dim=4)
    index_path = vector_dir / "doc_vectors.idx"
    original_bytes = index_path.read_bytes()

    def write_partial_temp_then_fail(path: str) -> None:
        Path(path).write_bytes(b"injected partial temp index")
        raise OSError("injected hnsw temp save failure")

    fake_index = SimpleNamespace(save_index=write_partial_temp_then_fail)
    with patch.object(store, "doc_index", fake_index):
        with pytest.raises(OSError, match="hnsw temp save failure"):
            store._save_index("doc_vectors")

    assert index_path.read_bytes() == original_bytes
    assert list(vector_dir.glob(f".{index_path.name}.*.tmp")) == []


def test_migration_cas_does_not_overwrite_concurrent_metadata_update(
    tmp_path: Path,
):
    """迁移快照过期时安全报错，并保留并发 writer 的完整字节。"""
    vector_dir = tmp_path / "vectors"
    vector_dir.mkdir()
    metadata_path = vector_dir / "doc_vectors_metadata.json"
    endpoint = "https://embd.example.com/v1"
    original = {
        "dim": 4,
        "embedding_fingerprint": _legacy_fingerprint(endpoint),
        "id_mapping": {},
    }
    concurrent = {
        **original,
        "id_mapping": {"10000": [1, 0]},
    }
    metadata_path.write_text(json.dumps(original), encoding="utf-8")
    concurrent_bytes = json.dumps(concurrent, separators=(",", ":")).encode("utf-8")
    real_fsync = os.fsync
    mutated = False

    def fsync_then_concurrently_update(file_descriptor: int):
        nonlocal mutated
        real_fsync(file_descriptor)
        if not mutated:
            metadata_path.write_bytes(concurrent_bytes)
            mutated = True

    with patch(
        "src.storage.vector_store.os.fsync",
        side_effect=fsync_then_concurrently_update,
    ):
        with pytest.raises(RuntimeError, match="安全迁移失败"):
            VectorStore(vector_dir, dim=4)

    assert metadata_path.read_bytes() == concurrent_bytes
    assert list(vector_dir.glob(f".{metadata_path.name}.*.tmp")) == []


@pytest.mark.parametrize("mode", ["expected_bytes", "require_missing"])
def test_atomic_writer_lock_serializes_real_concurrent_writers(
    tmp_path: Path,
    mode: str,
):
    """CAS check 与 replace 必须处于同一跨 writer 临界区。"""
    target = tmp_path / "metadata.json"
    original_bytes: bytes | None = None
    if mode == "expected_bytes":
        target.write_text('{"generation":0}', encoding="utf-8")
        original_bytes = target.read_bytes()

    barrier = threading.Barrier(2)
    payloads = ({"generation": 1}, {"generation": 2})

    def write(payload: dict[str, int]) -> str:
        barrier.wait()
        try:
            VectorStore._atomic_write_json(
                target,
                payload,
                expected_bytes=original_bytes,
                require_missing=mode == "require_missing",
            )
        except RuntimeError:
            return "conflict"
        return "written"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, payloads))

    assert sorted(outcomes) == ["conflict", "written"]
    assert json.loads(target.read_text(encoding="utf-8")) in payloads
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []


def test_concurrent_initialization_failure_cannot_delete_successful_pair(
    tmp_path: Path,
):
    """失败 creator 清理须在配对锁内完成，不能删除随后成功方的共享 idx。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    real_dump = json.dump
    dump_lock = threading.Lock()
    dump_count = 0
    start = threading.Barrier(2)

    def fail_first_dump(payload, file, **kwargs):
        nonlocal dump_count
        with dump_lock:
            dump_count += 1
            current_count = dump_count
        if current_count == 1:
            raise OSError("injected first creator failure")
        return real_dump(payload, file, **kwargs)

    def construct() -> str:
        start.wait()
        try:
            VectorStore(vector_dir, dim=4)
        except OSError:
            return "failed"
        return "created"

    with (
        patch("src.storage.vector_store.get_config", return_value=config),
        patch("src.storage.vector_store.json.dump", side_effect=fail_first_dump),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        outcomes = list(executor.map(lambda _: construct(), range(2)))

    assert sorted(outcomes) == ["created", "failed"]
    for name in ("doc_vectors", "chunk_vectors"):
        assert (vector_dir / f"{name}.idx").exists()
        assert (vector_dir / f"{name}_metadata.json").exists()
    with patch("src.storage.vector_store.get_config", return_value=config):
        recovered = VectorStore(vector_dir, dim=4)
    assert recovered.get_index_stats()["chunk_count"] == 0


def test_two_open_instances_merge_sequential_chunk_writes(tmp_path: Path):
    """第二实例必须在配对锁内重载磁盘 index，不能用 stale 内存覆盖第一条。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        first = VectorStore(vector_dir, dim=4)
        second = VectorStore(vector_dir, dim=4)
        first.add_chunk_vector(1, 0, np.ones(4, dtype=np.float32))
        second.add_chunk_vector(1, 1, np.arange(4, dtype=np.float32))
        reopened = VectorStore(vector_dir, dim=4)

    assert reopened.get_index_stats()["chunk_count"] == 2
    assert reopened.get_chunk_indices_for_entry(1) == [0, 1]
    metadata = json.loads(
        (vector_dir / "chunk_vectors_metadata.json").read_text(encoding="utf-8")
    )
    assert set(metadata["id_mapping"]) == {"10000", "10001"}


def test_two_open_instances_merge_concurrent_chunk_writes(tmp_path: Path):
    """真实并发写也须同时保留两条 metadata mapping 与两条磁盘向量。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        first = VectorStore(vector_dir, dim=4)
        second = VectorStore(vector_dir, dim=4)

    start = threading.Barrier(2)

    def add(store: VectorStore, chunk_index: int) -> None:
        start.wait()
        store.add_chunk_vector(
            1,
            chunk_index,
            np.full(4, chunk_index + 1, dtype=np.float32),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(add, first, 0),
            executor.submit(add, second, 1),
        ]
        for future in futures:
            future.result()

    with patch("src.storage.vector_store.get_config", return_value=config):
        reopened = VectorStore(vector_dir, dim=4)
    assert reopened.get_index_stats()["chunk_count"] == 2
    assert reopened.get_chunk_indices_for_entry(1) == [0, 1]


def test_hash_in_v1_mismatch_preserves_rollback_fingerprint(tmp_path: Path):
    """A→B mismatch 前不得删除 hash-in-v1 或把失败伪装成缺失指纹。"""
    vector_dir = tmp_path / "vectors"
    endpoint_a = "https://embd-a.example.com/v1"
    endpoint_b = "https://embd-b.example.com/v1"
    config_a = _fake_config(endpoint_a, "model-a", 4)
    config_b = _fake_config(endpoint_b, "model-a", 4)
    transition = {
        "schema_version": VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION,
        "base_url_sha256": endpoint_contract_sha256(endpoint_a),
        "embedding_model": "model-a",
        "embedding_dim": "4",
        "base_url": endpoint_a,
    }

    with patch("src.storage.vector_store.get_config", return_value=config_a):
        VectorStore(vector_dir, dim=4)
    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
        metadata["embedding_fingerprint"] = transition
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with patch("src.storage.vector_store.get_config", return_value=config_b):
        with pytest.raises(RuntimeError, match="Embedding 索引契约不匹配"):
            VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["embedding_fingerprint"] == transition
        assert metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY][
            "base_url_sha256"
        ] == endpoint_contract_sha256(endpoint_a)


@pytest.mark.parametrize(
    ("location", "malformed_version"),
    (
        ("metadata", 2.5),
        ("metadata", "3.0"),
        ("v2", 2.5),
        ("legacy", "3.0"),
    ),
)
def test_malformed_schema_version_fails_before_any_rewrite(
    tmp_path: Path,
    location: str,
    malformed_version: object,
):
    """schema_version 只接受 JSON integer，禁止 int() 式宽松降级。"""
    vector_dir = tmp_path / "vectors"
    endpoint = "https://embd.example.com/v1"
    config = _fake_config(endpoint, "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    doc_path = vector_dir / "doc_vectors_metadata.json"
    metadata = json.loads(doc_path.read_text(encoding="utf-8"))
    if location == "metadata":
        metadata["schema_version"] = malformed_version
    elif location == "v2":
        metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY]["schema_version"] = (
            malformed_version
        )
    else:
        metadata["embedding_fingerprint"]["schema_version"] = malformed_version
    doc_path.write_text(json.dumps(metadata), encoding="utf-8")

    original_bytes = {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    }
    with pytest.raises(RuntimeError, match="畸形"):
        VectorStore(vector_dir, dim=4)

    assert {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    } == original_bytes


def test_present_non_dict_legacy_fingerprint_fails_closed(tmp_path: Path):
    """legacy 键存在但非 object 时不能清理成 missing 后走兼容 warning。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config(
        "https://bad-user:bad-password@embd.example.com/v1",
        "model-a",
        4,
    )
    safe_config = _fake_config("https://embd.example.com/v1", "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=safe_config):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
        metadata.pop("schema_version", None)
        metadata["embedding_fingerprint"] = "not-an-object"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    original_bytes = {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    }

    with patch("src.storage.vector_store.get_config", return_value=config):
        with pytest.raises(RuntimeError, match="畸形"):
            VectorStore(vector_dir, dim=4)

    assert {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    } == original_bytes


def test_conflicting_v2_and_raw_v1_fingerprints_fail_before_rewrite(
    tmp_path: Path,
):
    """v2=A/raw-v1=B 时新旧 reader 不得各自接受不同契约。"""
    vector_dir = tmp_path / "vectors"
    endpoint_a = "https://embd-a.example.com/v1"
    endpoint_b = "https://embd-b.example.com/v1"
    config = _fake_config(endpoint_a, "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    doc_path = vector_dir / "doc_vectors_metadata.json"
    metadata = json.loads(doc_path.read_text(encoding="utf-8"))
    metadata["embedding_fingerprint"] = _legacy_fingerprint(endpoint_b)
    doc_path.write_text(json.dumps(metadata), encoding="utf-8")
    original_bytes = {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    }

    with patch("src.storage.vector_store.get_config", return_value=config):
        with pytest.raises(RuntimeError, match="冲突"):
            VectorStore(vector_dir, dim=4)

    assert {
        path.name: path.read_bytes()
        for path in vector_dir.glob("*_metadata.json")
    } == original_bytes


@pytest.mark.parametrize("future_location", ["metadata", "v2", "hash_in_v1"])
def test_future_schema_fails_before_rewriting_either_metadata_file(
    tmp_path: Path,
    future_location: str,
):
    """任一 future schema 都须在 doc/chunk 的任何迁移写之前整体拒绝。"""
    vector_dir = tmp_path / "vectors"
    endpoint = "https://embd.example.com/v1"
    config = _fake_config(endpoint, "model-a", 4)
    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    doc_path = vector_dir / "doc_vectors_metadata.json"
    doc_metadata = json.loads(doc_path.read_text(encoding="utf-8"))
    if future_location == "metadata":
        doc_metadata["schema_version"] = VectorStore.METADATA_SCHEMA_VERSION + 1
    elif future_location == "v2":
        doc_metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY]["schema_version"] = (
            VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION + 1
        )
    else:
        future_fingerprint = dict(
            doc_metadata[VectorStore.EMBEDDING_FINGERPRINT_V2_KEY]
        )
        future_fingerprint["schema_version"] = (
            VectorStore.EMBEDDING_FINGERPRINT_SCHEMA_VERSION + 1
        )
        doc_metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY)
        doc_metadata["embedding_fingerprint"] = future_fingerprint
    doc_path.write_text(json.dumps(doc_metadata), encoding="utf-8")

    chunk_path = vector_dir / "chunk_vectors_metadata.json"
    chunk_metadata = json.loads(chunk_path.read_text(encoding="utf-8"))
    chunk_metadata.pop("schema_version", None)
    chunk_metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
    chunk_metadata["embedding_fingerprint"] = _legacy_fingerprint(
        "https://future-test-user:future-test-password@embd.example.com/v1"
    )
    chunk_path.write_text(json.dumps(chunk_metadata), encoding="utf-8")

    original_bytes = {
        doc_path.name: doc_path.read_bytes(),
        chunk_path.name: chunk_path.read_bytes(),
    }
    with patch("src.storage.vector_store.get_config", return_value=config):
        with pytest.raises(RuntimeError, match="高于当前版本"):
            VectorStore(vector_dir, dim=4)

    assert doc_path.read_bytes() == original_bytes[doc_path.name]
    assert chunk_path.read_bytes() == original_bytes[chunk_path.name]
    assert list(vector_dir.glob(".*_metadata.json.*.tmp")) == []


def test_vector_store_loads_legacy_metadata_without_embedding_fingerprint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    """旧索引缺少契约指纹时兼容加载，但必须可观测。"""
    vector_dir = tmp_path / "vectors"

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-a", 4),
    ):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text())
        metadata.pop("embedding_fingerprint", None)
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
        metadata.pop("schema_version", None)
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with patch(
        "src.storage.vector_store.get_config",
        return_value=_fake_config("https://embd.example.com/v1", "model-b", 4),
    ):
        store = VectorStore(vector_dir, dim=4)

    assert store.dim == 4
    assert "缺少 Embedding 契约指纹" in caplog.text


def test_current_schema_missing_v2_fingerprint_is_rejected(tmp_path: Path):
    """现代 schema 缺少 v2 指纹时不得沿用 legacy warning 静默复用。"""
    vector_dir = tmp_path / "vectors"
    config = _fake_config("https://embd.example.com/v1", "model-a", 4)

    with patch("src.storage.vector_store.get_config", return_value=config):
        VectorStore(vector_dir, dim=4)

    for metadata_path in vector_dir.glob("*_metadata.json"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.pop("embedding_fingerprint", None)
        metadata.pop(VectorStore.EMBEDDING_FINGERPRINT_V2_KEY, None)
        assert metadata["schema_version"] == VectorStore.METADATA_SCHEMA_VERSION
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with patch("src.storage.vector_store.get_config", return_value=config):
        with pytest.raises(RuntimeError, match="当前 metadata schema 缺少"):
            VectorStore(vector_dir, dim=4)
