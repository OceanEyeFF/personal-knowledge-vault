"""Contract tests for the W4 empty semantic-index fixture.

The module intentionally does not import the product source tree.  It validates
the checked-in binary bundle using only stdlib, pytest, and the release-pinned
hnswlib package.
"""

from __future__ import annotations

import hashlib
import importlib.util
from importlib import metadata as importlib_metadata
import json
from pathlib import Path
from types import ModuleType

import hnswlib
import pytest


pytestmark = pytest.mark.artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "w4" / (
    "semantic-vector-index.v1"
)
MANIFEST_PATH = BUNDLE_ROOT / "manifest.v1.json"
GENERATOR_PATH = BUNDLE_ROOT / "generate_fixture.py"
PUBLISHED_CONFIG_PATH = REPOSITORY_ROOT / "config" / "config.yaml"
SCENARIO_MODULE_PATH = (
    REPOSITORY_ROOT / "packaging" / "w4_driver" / "W4.Scenarios.psm1"
)
PAIR_NAMES = ("doc_vectors", "chunk_vectors")
EXPECTED_FILES = {
    f"{pair_name}{suffix}"
    for pair_name in PAIR_NAMES
    for suffix in (".idx", "_metadata.json")
}
EXPECTED_SCHEMA = "pkv.m13.w4-semantic-index.v1"
EXPECTED_HNSWLIB_VERSION = "0.8.0"
EXPECTED_ENDPOINT = "https://api.openai.com/v1"
EXPECTED_MODEL = "text-embedding-3-small"
EXPECTED_DIMENSION = 1536


def test_artifact_driver_reads_the_versioned_semantic_manifest() -> None:
    source = SCENARIO_MODULE_PATH.read_text(encoding="utf-8-sig")
    assert "Join-Path $bundle 'manifest.v1.json'" in source
    assert "Join-Path $bundle 'manifest.json'" not in source
    assert "Semantic vector fixture file inventory is not the exact canonical set/order" in source
    assert "Test-W4PathContainedBy -Candidate $source -Root $bundleFull" in source
    assert "Test-W4PathContainedBy -Candidate $destination -Root $targetFull" in source
    assert "[void](Get-W4TreeManifest -Root $bundle)" in source


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(root: Path = BUNDLE_ROOT) -> dict:
    payload = json.loads((root / "manifest.v1.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "pkv_w4_semantic_fixture_generator",
        GENERATOR_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_hashes(root: Path) -> dict[str, str]:
    payload = _manifest(root)
    return {record["path"]: record["sha256"] for record in payload["files"]}


def _published_embedding_defaults() -> dict[str, object]:
    """Read the three scalar defaults without importing product configuration."""

    values: dict[str, object] = {}
    in_embedding_block = False
    for line in PUBLISHED_CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        if line == "  embedding:":
            in_embedding_block = True
            continue
        if not in_embedding_block:
            continue
        if line and not line.startswith("    "):
            break
        stripped = line.strip()
        for key in ("base_url", "model", "dim"):
            prefix = f"{key}:"
            if stripped.startswith(prefix):
                raw_value = stripped[len(prefix) :].strip()
                values[key] = int(raw_value) if key == "dim" else json.loads(raw_value)
    assert set(values) == {"base_url", "model", "dim"}
    return values


def test_manifest_inventory_and_exact_hashes() -> None:
    manifest = _manifest()
    assert manifest["schema_version"] == EXPECTED_SCHEMA
    assert manifest["synthetic_only"] is True
    assert manifest["contains_credentials"] is False
    assert manifest["contains_real_vault_data"] is False
    assert manifest["toolchain"] == {"hnswlib": EXPECTED_HNSWLIB_VERSION}

    records = manifest["files"]
    assert isinstance(records, list)
    assert all(set(record) == {"path", "sha256"} for record in records)
    assert [record["path"] for record in records] == sorted(EXPECTED_FILES)
    assert len({record["path"] for record in records}) == len(records)
    for record in records:
        assert len(record["sha256"]) == 64
        assert set(record["sha256"]) <= set("0123456789abcdef")
        assert _sha256(BUNDLE_ROOT / record["path"]) == record["sha256"]


def test_default_embedding_contract_and_complete_metadata_pairs() -> None:
    manifest = _manifest()
    endpoint_hash = hashlib.sha256(EXPECTED_ENDPOINT.encode("utf-8")).hexdigest()
    assert _published_embedding_defaults() == {
        "base_url": EXPECTED_ENDPOINT,
        "model": EXPECTED_MODEL,
        "dim": EXPECTED_DIMENSION,
    }
    assert manifest["embedding_contract"] == {
        "base_url": EXPECTED_ENDPOINT,
        "base_url_sha256": endpoint_hash,
        "model": EXPECTED_MODEL,
        "dim": EXPECTED_DIMENSION,
        "fingerprint_schema_version": 2,
    }
    assert manifest["index_contract"] == {
        "space": "cosine",
        "dim": EXPECTED_DIMENSION,
        "generation_max_elements": 10_000,
        "loaded_empty_max_elements": 0,
        "M": 16,
        "ef_construction": 200,
        "random_seed": 100,
        "element_count": 0,
        "pairs": list(PAIR_NAMES),
    }

    expected_metadata: dict | None = None
    for pair_name in PAIR_NAMES:
        assert (BUNDLE_ROOT / f"{pair_name}.idx").is_file()
        metadata_path = BUNDLE_ROOT / f"{pair_name}_metadata.json"
        assert metadata_path.is_file()
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["schema_version"] == 2
        assert metadata["dim"] == EXPECTED_DIMENSION
        assert metadata["space"] == "cosine"
        assert metadata["M"] == 16
        assert metadata["ef_construction"] == 200
        assert metadata["id_mapping"] == {}
        assert metadata["embedding_fingerprint_v2"] == {
            "schema_version": 2,
            "base_url_sha256": endpoint_hash,
            "embedding_model": EXPECTED_MODEL,
            "embedding_dim": str(EXPECTED_DIMENSION),
        }
        assert metadata["embedding_fingerprint"] == {
            "base_url": EXPECTED_ENDPOINT,
            "embedding_model": EXPECTED_MODEL,
            "embedding_dim": str(EXPECTED_DIMENSION),
        }
        if expected_metadata is None:
            expected_metadata = metadata
        else:
            assert metadata == expected_metadata


def test_both_empty_indexes_load_with_release_pinned_hnswlib() -> None:
    assert importlib_metadata.version("hnswlib") == EXPECTED_HNSWLIB_VERSION
    for pair_name in PAIR_NAMES:
        index = hnswlib.Index(space="cosine", dim=EXPECTED_DIMENSION)
        index.load_index(
            str(BUNDLE_ROOT / f"{pair_name}.idx"),
            allow_replace_deleted=True,
        )
        assert index.dim == EXPECTED_DIMENSION
        assert index.space == "cosine"
        assert index.element_count == 0
        # hnswlib 0.8.0 does not persist unused capacity for an empty index.
        # VectorStore follows the same deterministic repair immediately after
        # load, before semantic retrieval reaches the Provider seam.
        assert index.max_elements == 0
        index.resize_index(10_000)
        assert index.max_elements == 10_000


def test_generator_is_byte_reproducible_and_matches_checked_bundle(tmp_path: Path) -> None:
    assert importlib_metadata.version("hnswlib") == EXPECTED_HNSWLIB_VERSION
    generator = _load_generator()
    first = tmp_path / "first"
    second = tmp_path / "second"
    generator.generate_bundle(first)
    generator.generate_bundle(second)

    assert _manifest_hashes(first) == _manifest_hashes(second) == _manifest_hashes(
        BUNDLE_ROOT
    )
    compared_names = sorted(EXPECTED_FILES | {"manifest.v1.json"})
    for name in compared_names:
        checked_in = (BUNDLE_ROOT / name).read_bytes()
        assert (first / name).read_bytes() == checked_in
        assert (second / name).read_bytes() == checked_in
