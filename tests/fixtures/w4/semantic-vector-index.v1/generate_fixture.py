"""Generate the deterministic W4 empty semantic-index fixture bundle.

This script is deliberately independent of the product source tree.  Its only
non-stdlib dependency is the release-pinned hnswlib build used to create and
load the checked-in index files.
"""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata as importlib_metadata
import json
from pathlib import Path
from typing import Any

import hnswlib


SCHEMA_VERSION = "pkv.m13.w4-semantic-index.v1"
HNSWLIB_VERSION = "0.8.0"
DIMENSION = 1536
SPACE = "cosine"
MAX_ELEMENTS = 10_000
M = 16
EF_CONSTRUCTION = 200
RANDOM_SEED = 100
DEFAULT_EMBEDDING_ENDPOINT = "https://api.openai.com/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
PAIR_NAMES = ("doc_vectors", "chunk_vectors")
MANIFEST_NAME = "manifest.v1.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _embedding_fingerprint_v2() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "base_url_sha256": hashlib.sha256(
            DEFAULT_EMBEDDING_ENDPOINT.encode("utf-8")
        ).hexdigest(),
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "embedding_dim": str(DIMENSION),
    }


def _metadata_payload() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "dim": DIMENSION,
        "space": SPACE,
        "M": M,
        "ef_construction": EF_CONSTRUCTION,
        "embedding_fingerprint_v2": _embedding_fingerprint_v2(),
        "embedding_fingerprint": {
            "base_url": DEFAULT_EMBEDDING_ENDPOINT,
            "embedding_model": DEFAULT_EMBEDDING_MODEL,
            "embedding_dim": str(DIMENSION),
        },
        "id_mapping": {},
    }


def _assert_toolchain() -> None:
    installed = importlib_metadata.version("hnswlib")
    if installed != HNSWLIB_VERSION:
        raise RuntimeError(
            "semantic fixture generation requires "
            f"hnswlib=={HNSWLIB_VERSION}; found {installed}"
        )


def _generate_empty_index(path: Path) -> None:
    index = hnswlib.Index(space=SPACE, dim=DIMENSION)
    index.init_index(
        max_elements=MAX_ELEMENTS,
        M=M,
        ef_construction=EF_CONSTRUCTION,
        random_seed=RANDOM_SEED,
        allow_replace_deleted=True,
    )
    index.save_index(str(path))


def generate_bundle(output_dir: Path) -> dict[str, Any]:
    """Generate both complete pairs and their exact-hash bundle manifest."""

    _assert_toolchain()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    generated_paths: list[Path] = []
    for pair_name in PAIR_NAMES:
        index_path = output_dir / f"{pair_name}.idx"
        metadata_path = output_dir / f"{pair_name}_metadata.json"
        _generate_empty_index(index_path)
        _write_json(metadata_path, _metadata_payload())
        generated_paths.extend((index_path, metadata_path))

    fingerprint = _embedding_fingerprint_v2()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "synthetic_only": True,
        "contains_credentials": False,
        "contains_real_vault_data": False,
        "toolchain": {
            "hnswlib": HNSWLIB_VERSION,
        },
        "index_contract": {
            "space": SPACE,
            "dim": DIMENSION,
            "generation_max_elements": MAX_ELEMENTS,
            "loaded_empty_max_elements": 0,
            "M": M,
            "ef_construction": EF_CONSTRUCTION,
            "random_seed": RANDOM_SEED,
            "element_count": 0,
            "pairs": list(PAIR_NAMES),
        },
        "embedding_contract": {
            "base_url": DEFAULT_EMBEDDING_ENDPOINT,
            "base_url_sha256": fingerprint["base_url_sha256"],
            "model": DEFAULT_EMBEDDING_MODEL,
            "dim": DIMENSION,
            "fingerprint_schema_version": fingerprint["schema_version"],
        },
        "files": [
            {"path": path.name, "sha256": _sha256(path)}
            for path in sorted(generated_paths, key=lambda item: item.name)
        ],
        "generation": {
            "script": "generate_fixture.py",
            "network_required": False,
            "source_data_required": False,
            "notes": (
                "Empty indexes are regenerated with fixed hnswlib parameters; "
                "hnswlib serializes an empty index with loaded max_elements=0; "
                "the product load path resizes it to generation_max_elements. "
                "The artifact contract test requires byte-identical outputs."
            ),
        },
    }
    _write_json(output_dir / MANIFEST_NAME, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the deterministic W4 semantic vector-index fixture"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="bundle output directory (defaults to this script's directory)",
    )
    args = parser.parse_args()
    manifest = generate_bundle(args.output.resolve())
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
