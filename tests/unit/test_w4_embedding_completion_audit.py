"""W4 durable completion evidence and retry behavior, using synthetic logs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.runtime.audit import AuditTrace, AuditTraceError
from src.runtime.embedding_completion_audit import (
    append_completion,
    completion_id,
    inspect_completion,
)
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope


IDENTITY = {
    "operation_id": "embedding-rebuild-" + "1" * 32,
    "generation_id": "g-" + "2" * 20 + "-" + "3" * 12,
    "manifest_sha256": "4" * 64,
}


def _layout(tmp_path: Path) -> RuntimeLayout:
    resources = tmp_path / "resources"
    resources.mkdir()
    return RuntimeLayout.resolve(resources_root=resources, user_data_root=tmp_path / "data", environment={})


def _record(event: dict, *, recorded_at: str = "2026-10-25T00:00:00Z") -> bytes:
    return (json.dumps({"schema_version": 1, "recorded_at": recorded_at, "event": event}) + "\n").encode()


def _completed(**changes) -> dict:
    return {
        "operation": "embedding_rebuild",
        "phase": "completed",
        "context": {"plan_id": "5" * 64},
        "result": {**IDENTITY, "completion_id": completion_id(**IDENTITY), **changes},
    }


def _write(layout: RuntimeLayout, data: bytes) -> Path:
    path = layout.log_dir / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _inspect(layout: RuntimeLayout, *, publication_verified: bool = True) -> bool:
    return inspect_completion(layout, **IDENTITY, publication_verified=publication_verified)


def _append(layout: RuntimeLayout, **changes) -> bool:
    return append_completion(
        layout, **IDENTITY, publication_verified=True,
        context={"note": "configured-secret", "api_key": "configured-secret"},
        secret_values=("configured-secret",), **changes,
    )


def _assert_repair(call) -> None:
    with pytest.raises(PKVRuntimeError) as captured:
        call()
    assert captured.value.code is ErrorCode.REPAIR_REQUIRED
    assert captured.value.__cause__ is None
    assert "private-audit-body" not in str(captured.value)


def test_completion_id_uses_frozen_domain_and_canonical_encoding() -> None:
    payload = (
        '{"generation_id":"g-22222222222222222222-333333333333",'
        '"manifest_sha256":"' + "4" * 64 + '",'
        '"operation_id":"embedding-rebuild-' + "1" * 32 + '"}'
    )
    expected = hashlib.sha256(b"pkv.embedding.audit-completion.v1\0" + payload.encode()).hexdigest()
    assert completion_id(**IDENTITY) == expected


def test_missing_log_inspection_does_not_create_root_or_acquire_lease(tmp_path: Path, monkeypatch) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setattr("src.runtime.write_lease.write_lease_scope", lambda *a, **k: pytest.fail("read acquired lease"))
    assert not _inspect(layout)
    assert not layout.user_data_root.exists()


def test_modern_completion_is_recognized_after_fresh_reader_and_timestamp_change(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    event = _completed()
    path = _write(layout, _record(event) + _record(event, recorded_at="2026-10-26T03:04:05+00:00"))
    before = path.read_bytes()
    fresh = RuntimeLayout.resolve(resources_root=layout.resources_root, user_data_root=layout.user_data_root, environment={})
    assert _inspect(fresh, publication_verified=False)
    assert path.read_bytes() == before


@pytest.mark.parametrize("data", [
    b'{"private-audit-body":',
    _record(_completed()).rstrip(b"\n"),
    b"\xff\n",
    b"\n",
    b"[]\n",
    b'{"schema_version":1,"schema_version":1,"recorded_at":"2026-10-25T00:00:00Z","event":{}}\n',
    b'{"schema_version":1,"recorded_at":"2026-10-25T00:00:00Z","event":{"value":NaN}}\n',
    b'{"schema_version":1,"recorded_at":"2026-10-25T00:00:00Z","event":{"value":1e999}}\n',
    b'{"schema_version":2,"recorded_at":"2026-10-25T00:00:00Z","event":{}}\n',
    b'{"schema_version":1,"recorded_at":"2026-10-25T00:00:00","event":{}}\n',
])
def test_unreadable_format_is_repair_even_after_matching_record(tmp_path: Path, data: bytes) -> None:
    layout = _layout(tmp_path)
    path = _write(layout, _record(_completed()) + data)
    before = path.read_bytes()
    _assert_repair(lambda: _inspect(layout))
    assert path.read_bytes() == before


@pytest.mark.parametrize("changes", [
    {"completion_id": "f" * 64},
    {"generation_id": "g-conflicting-identity"},
    {"manifest_sha256": "f" * 64},
    {"operation_id": "embedding-rebuild-other-operation"},
    {"completion_id": None},
])
def test_conflicting_completion_identity_cannot_replace_matching_record(tmp_path: Path, changes: dict) -> None:
    layout = _layout(tmp_path)
    _write(layout, _record(_completed()) + _record(_completed(**changes)))
    _assert_repair(lambda: _inspect(layout))


@pytest.mark.parametrize("event_changes", [{"phase": "failed"}, {"operation": "other_operation"}])
def test_same_completion_identity_with_different_event_type_is_repair(tmp_path: Path, event_changes: dict) -> None:
    layout = _layout(tmp_path)
    _write(layout, _record({**_completed(), **event_changes}))
    _assert_repair(lambda: _inspect(layout))


def test_different_valid_identity_does_not_prove_requested_completion(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    other = {**IDENTITY, "operation_id": "embedding-rebuild-other-operation"}
    _write(layout, _record(_completed(**other, completion_id=completion_id(**other))))
    assert not _inspect(layout)


def test_same_completion_identity_cannot_disagree_on_previous_generation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _write(layout, _record(_completed(previous_generation="g-old-first")) + _record(_completed(previous_generation="g-old-other")))
    _assert_repair(lambda: _inspect(layout))


@pytest.mark.parametrize("verified,include_intent,expected", [(False, True, False), (True, False, False), (True, True, True)])
def test_legacy_completion_requires_matching_intent_and_publication(tmp_path: Path, verified: bool, include_intent: bool, expected: bool) -> None:
    layout = _layout(tmp_path)
    event = _completed()
    del event["result"]["completion_id"]
    del event["result"]["manifest_sha256"]
    intent = {"operation": "embedding_rebuild", "phase": "activation_intent", **IDENTITY}
    _write(layout, (_record(intent) if include_intent else b"") + _record(event))
    assert _inspect(layout, publication_verified=verified) is expected


def test_legacy_conflicting_activation_manifest_is_repair(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    event = _completed()
    del event["result"]["completion_id"]
    intent = {"operation": "embedding_rebuild", "phase": "activation_intent", **IDENTITY, "manifest_sha256": "e" * 64}
    _write(layout, _record(intent) + _record(event))
    _assert_repair(lambda: _inspect(layout))


def test_io_failure_is_repair_and_never_reflects_log_payload(tmp_path: Path, monkeypatch) -> None:
    layout = _layout(tmp_path)
    _write(layout, _record(_completed()))
    def fail_open(*args, **kwargs):
        raise PermissionError("private-audit-body")
    monkeypatch.setattr(RuntimeLayout, "open_user_file", fail_open)
    _assert_repair(lambda: _inspect(layout))


def test_audit_hardlink_is_refused_without_reading_other_file(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    target = _write(layout, _record(_completed()))
    alias = tmp_path / "owned-synthetic-audit.jsonl"
    os.link(target, alias)
    _assert_repair(lambda: _inspect(layout))


def test_append_requires_active_lease_before_creating_root(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with pytest.raises(PKVRuntimeError) as captured:
        _append(layout)
    assert captured.value.code is ErrorCode.WRITE_BUSY
    assert not layout.user_data_root.exists()


def test_append_requires_revalidated_publication(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with write_lease_scope(layout):
        _assert_repair(lambda: append_completion(layout, **IDENTITY, publication_verified=False, context={}, secret_values=()))
    assert not (layout.log_dir / "audit.jsonl").exists()


def test_reconcile_appends_once_and_preserves_redaction(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    with write_lease_scope(layout):
        assert _append(layout)
    path = layout.log_dir / "audit.jsonl"
    first = path.read_bytes()
    with write_lease_scope(layout):
        assert not _append(layout)
    assert path.read_bytes() == first
    assert b"configured-secret" not in first
    assert json.loads(first)["event"]["result"]["completion_id"] == completion_id(**IDENTITY)


def test_retry_after_successful_fsync_lost_response_adds_no_second_record(tmp_path: Path, monkeypatch) -> None:
    layout = _layout(tmp_path)
    original = AuditTrace.append
    def lost_response(self, event):
        original(self, event)
        raise AuditTraceError()
    with monkeypatch.context() as patch:
        patch.setattr(AuditTrace, "append", lost_response)
        with write_lease_scope(layout), pytest.raises(AuditTraceError):
            _append(layout)
    fresh = RuntimeLayout.resolve(resources_root=layout.resources_root, user_data_root=layout.user_data_root, environment={})
    with write_lease_scope(fresh):
        assert not _append(fresh)
    assert len((layout.log_dir / "audit.jsonl").read_bytes().splitlines()) == 1
