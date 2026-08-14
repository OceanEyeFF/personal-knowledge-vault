"""Artifact resource allowlist must never collect user or secret material."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent.parent
MANIFEST_PATH = PROJECT_ROOT / "packaging" / "runtime-resources.json"
pytestmark = pytest.mark.packaging_contract


def test_runtime_resource_manifest_expands_to_required_read_only_files() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    included = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for pattern in manifest["include_globs"]
        for path in PROJECT_ROOT.glob(pattern)
        if path.is_file()
    }

    assert "config/config.yaml" in included
    assert "config/custom_dict.txt" in included
    assert any(path.startswith("config/workflows/") for path in included)
    assert any(path.startswith("scripts/migrations/") for path in included)
    assert any(path.startswith("src/ai/prompts/") for path in included)
    assert not any(path.startswith("src/gui/") for path in included)


def test_manifest_includes_neither_secrets_nor_mutable_user_artifacts() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    included = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for pattern in manifest["include_globs"]
        for path in PROJECT_ROOT.glob(pattern)
        if path.is_file()
    }
    forbidden_names = {".env", "local.yaml"}
    forbidden_suffixes = {".db", ".sqlite", ".sqlite3", ".log", ".idx"}

    assert included
    assert all(Path(path).name not in forbidden_names for path in included)
    assert all(Path(path).suffix.lower() not in forbidden_suffixes for path in included)
    assert all(not path.startswith((".data/", ".data-test/", "vault/", "tmp/")) for path in included)
    assert "**/local.yaml" in manifest["forbidden_globs"]
    assert ".data/**" in manifest["forbidden_globs"]
