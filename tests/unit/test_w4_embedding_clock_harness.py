"""Fail-closed boundaries for the payload-external W4 budget clock request."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from tests.harness import w4_embedding_lifecycle as harness


def _fixture(tmp_path: Path, raw: bytes | None = None) -> tuple[dict[str, str], Path]:
    root = tmp_path / "product"
    clock = root / "tmp" / harness.CLOCK_FILENAME
    clock.parent.mkdir(parents=True)
    clock.write_bytes(
        raw
        if raw is not None
        else json.dumps(
            {"schema": harness.CLOCK_SCHEMA, "utc_now": harness.INITIAL_TIME}
        ).encode("utf-8")
    )
    return {"DATA_DIR": str(root), harness.CLOCK_ENV: str(clock)}, clock


def _consume(environment: dict[str, str], **overrides):
    return harness.consume_clock_request(
        environment,
        **{
            "target": "cli",
            "load_local": False,
            "synthetic_provider_config": True,
            **overrides,
        },
    )


def _forbidden(*_args, **_kwargs):
    raise AssertionError("the rejected clock request must not touch any file")


def _changed_stat(original: os.stat_result, **updates) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "st_mode": original.st_mode,
            "st_nlink": original.st_nlink,
            "st_size": original.st_size,
            "st_dev": original.st_dev,
            "st_ino": original.st_ino,
            "st_mtime_ns": original.st_mtime_ns,
            "st_file_attributes": getattr(original, "st_file_attributes", 0),
            **updates,
        }
    )


def test_absent_request_preserves_environment_without_filesystem_access(monkeypatch):
    environment = {"unrelated": "preserved"}
    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "lstat", _forbidden)
        patch.setattr(harness.os, "open", _forbidden)
        assert _consume(environment, target="pytest", load_local=True) is None
    assert environment == {"unrelated": "preserved"}


@pytest.mark.parametrize("target", ["cli", "mcp"])
def test_valid_clock_is_utc_and_consumed_before_product_entrypoint(tmp_path, target):
    environment, _ = _fixture(tmp_path)
    assert _consume(environment, target=target) == datetime(
        2026, 9, 30, 12, tzinfo=timezone.utc
    )
    assert harness.CLOCK_ENV not in environment
    assert set(environment) == {"DATA_DIR"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"load_local": True},
        {"synthetic_provider_config": False},
        {"target": "pytest"},
        {"target": "python"},
        {"target": "unknown"},
    ],
    ids=["live", "unvalidated-config", "pytest", "generic-python", "unknown"],
)
def test_invalid_child_scope_rejects_before_reading_any_configuration(
    monkeypatch, overrides
):
    # No DATA_DIR is provided: rejection must precede even path selection.
    environment = {harness.CLOCK_ENV: "untrusted-clock-request"}
    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "lstat", _forbidden)
        patch.setattr(harness.os, "open", _forbidden)
        with pytest.raises(RuntimeError, match="offline synthetic CLI/MCP"):
            _consume(environment, **overrides)
    assert environment == {}


@pytest.mark.parametrize(
    "selection", ["empty", "nul", "relative", "sibling", "other-leaf", "relative-root"]
)
def test_clock_path_is_fixed_before_any_file_is_read(tmp_path, monkeypatch, selection):
    environment, clock = _fixture(tmp_path)
    replacements = {
        "empty": "",
        "nul": "bad\x00path",
        "relative": "tmp/w4-clock.json",
        "sibling": str(tmp_path / "other" / "tmp" / harness.CLOCK_FILENAME),
        "other-leaf": str(clock.with_name("other-clock.json")),
    }
    if selection == "relative-root":
        environment["DATA_DIR"] = "relative-product-root"
    else:
        environment[harness.CLOCK_ENV] = replacements[selection]
    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "lstat", _forbidden)
        patch.setattr(harness.os, "open", _forbidden)
        with pytest.raises(RuntimeError, match="clock"):
            _consume(environment)
    assert harness.CLOCK_ENV not in environment


@pytest.mark.parametrize("component", ["leaf", "tmp", "root", "above-root"])
@pytest.mark.parametrize("link_kind", ["symlink", "reparse"])
def test_every_clock_ancestor_is_checked_without_following_links(
    tmp_path, monkeypatch, component, link_kind
):
    environment, clock = _fixture(tmp_path)
    selected = {
        "leaf": clock,
        "tmp": clock.parent,
        "root": clock.parent.parent,
        "above-root": tmp_path,
    }[component]
    actual_lstat = os.lstat

    def linked_lstat(path, *args, **kwargs):
        observed = actual_lstat(path, *args, **kwargs)
        if Path(path) == selected:
            if link_kind == "symlink":
                return _changed_stat(observed, st_mode=stat.S_IFLNK | 0o700)
            return _changed_stat(observed, st_file_attributes=0x400)
        return observed

    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "lstat", linked_lstat)
        patch.setattr(harness.os, "open", _forbidden)
        with pytest.raises(RuntimeError, match="contains a link"):
            _consume(environment)


@pytest.mark.parametrize(
    "updates",
    [
        {"st_nlink": 2},
        {"st_mode": stat.S_IFDIR | 0o700},
        {"st_size": 0},
        {"st_size": 4097},
    ],
    ids=["hardlink", "not-regular", "empty", "oversize"],
)
def test_clock_leaf_requires_one_bounded_regular_file(tmp_path, monkeypatch, updates):
    environment, clock = _fixture(tmp_path)
    actual_lstat = os.lstat

    def invalid_leaf(path, *args, **kwargs):
        observed = actual_lstat(path, *args, **kwargs)
        return _changed_stat(observed, **updates) if Path(path) == clock else observed

    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "lstat", invalid_leaf)
        patch.setattr(harness.os, "open", _forbidden)
        with pytest.raises(RuntimeError, match="bounded regular file"):
            _consume(environment)


def test_non_directory_ancestor_rejects_before_open(tmp_path, monkeypatch):
    environment, clock = _fixture(tmp_path)
    actual_lstat = os.lstat

    def invalid_parent(path, *args, **kwargs):
        observed = actual_lstat(path, *args, **kwargs)
        return (
            _changed_stat(observed, st_mode=stat.S_IFREG | 0o600)
            if Path(path) == clock.parent
            else observed
        )

    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "lstat", invalid_parent)
        patch.setattr(harness.os, "open", _forbidden)
        with pytest.raises(RuntimeError, match="ancestor is not a directory"):
            _consume(environment)


@pytest.mark.parametrize("failure", ["lstat", "open"])
def test_unreadable_clock_fails_closed(tmp_path, monkeypatch, failure):
    environment, clock = _fixture(tmp_path)
    original = getattr(os, failure)

    def deny_clock(path, *args, **kwargs):
        if Path(path) == clock:
            raise PermissionError("synthetic clock permission failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(harness.os, failure, deny_clock)
        with pytest.raises(RuntimeError, match="unavailable|opened safely"):
            _consume(environment)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b"[]",
        b"null",
        b'{"schema":"pkv.test.embedding-clock.v1","schema":"pkv.test.embedding-clock.v1","utc_now":"2026-09-30T12:00:00Z"}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":"2026-09-30T12:00:00Z","extra":true}',
        b'{"schema":"pkv.test.embedding-clock.v2","utc_now":"2026-09-30T12:00:00Z"}',
        b'{"schema":"pkv.test.embedding-clock.v1"}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":true}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":"2026-09-30T12:00:00"}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":"2026-09-30T12:00:00+00:00"}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":"2026-09-30T12:00:00.1Z"}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":"2026-02-30T12:00:00Z"}',
        b'{"schema":"pkv.test.embedding-clock.v1","utc_now":"2026-09-30T25:00:00Z"}',
    ],
    ids=[
        "truncated", "invalid-utf8", "array", "null", "duplicate", "extra",
        "future-schema", "missing", "non-string", "no-zone", "offset-zone",
        "fractional", "invalid-date", "invalid-hour",
    ],
)
def test_clock_payload_has_exact_json_and_utc_shape(tmp_path, raw):
    environment, _ = _fixture(tmp_path, raw)
    with pytest.raises(RuntimeError, match="W4 clock"):
        _consume(environment)
    assert harness.CLOCK_ENV not in environment


@pytest.mark.parametrize("mutation", ["identity", "hardlink", "after-read"])
def test_clock_descriptor_changes_are_rejected(tmp_path, monkeypatch, mutation):
    environment, _ = _fixture(tmp_path)
    actual_fstat = os.fstat
    calls = 0

    def changed_fstat(descriptor):
        nonlocal calls
        observed = actual_fstat(descriptor)
        calls += 1
        if mutation == "identity":
            return _changed_stat(observed, st_ino=observed.st_ino + 1)
        if mutation == "hardlink":
            return _changed_stat(observed, st_nlink=2)
        if calls == 2:
            return _changed_stat(observed, st_mtime_ns=observed.st_mtime_ns + 1)
        return observed

    with monkeypatch.context() as patch:
        patch.setattr(harness.os, "fstat", changed_fstat)
        with pytest.raises(RuntimeError, match="changed during validation"):
            _consume(environment)
