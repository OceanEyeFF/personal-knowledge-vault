"""Durable, read-only completion lookup for explicit Embedding rebuilds.

The audit file is the authority for an appended completion, including after a
process lost the response to a successful fsync.  Publication itself remains a
separate fact that the lifecycle boundary must verify under its writer lease.
Only a boolean leaves this reader; retained article and Prompt payloads never
become part of an operation-status projection.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from src.runtime.audit import AUDIT_SCHEMA_VERSION, AuditTrace
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.writer_inventory import require_active_data_root_writer


_DOMAIN = b"pkv.embedding.audit-completion.v1\x00"
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _repair() -> PKVRuntimeError:
    return PKVRuntimeError(
        ErrorCode.REPAIR_REQUIRED,
        "Embedding 完成审计记录无法安全确认。",
        stage="embedding_completion_audit",
        recoverable=True,
    )


def _identity(operation_id: str, generation_id: str, manifest_sha256: str) -> dict[str, str]:
    if any(
        not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None
        for value in (operation_id, generation_id)
    ) or not isinstance(manifest_sha256, str) or _SHA256.fullmatch(manifest_sha256) is None:
        raise _repair()
    return {
        "operation_id": operation_id,
        "generation_id": generation_id,
        "manifest_sha256": manifest_sha256,
    }


def completion_id(*, operation_id: str, generation_id: str, manifest_sha256: str) -> str:
    """Compute the frozen domain-separated identity, without filesystem access."""

    payload = _identity(operation_id, generation_id, manifest_sha256)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(_DOMAIN + canonical.encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audit key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite audit value")


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def _event(line: bytes) -> dict[str, Any]:
    # AuditTrace always terminates a durable physical record with LF.  Even a
    # syntactically valid last object without LF may be an interrupted append.
    if not line.endswith(b"\n"):
        raise ValueError("incomplete audit line")
    record = json.loads(
        line.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
    )
    if (
        not isinstance(record, dict)
        or set(record) != {"schema_version", "recorded_at", "event"}
        or type(record["schema_version"]) is not int
        or record["schema_version"] != AUDIT_SCHEMA_VERSION
        or not isinstance(record["recorded_at"], str)
        or not isinstance(record["event"], dict)
        or not _finite(record)
    ):
        raise ValueError("unknown audit format")
    timestamp = datetime.fromisoformat(record["recorded_at"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("unknown audit timestamp")
    return record["event"]


def inspect_completion(
    layout: RuntimeLayout,
    *,
    operation_id: str,
    generation_id: str,
    manifest_sha256: str,
    publication_verified: bool,
) -> bool:
    """Read the complete no-follow JSONL and recognize exactly this completion.

    A modern completion is independently recognizable.  Legacy records require
    both a matching activation intent and a caller-verified publication fact.
    Missing files mean no record; every uncertain read or conflicting identity
    is repair-required.  This function never creates a directory or lease.
    """

    expected = _identity(operation_id, generation_id, manifest_sha256)
    expected_id = completion_id(**expected)
    modern = legacy = intent = False
    seen: dict[str, dict[str, Any]] = {}
    try:
        target = layout.validate_user_file(
            layout.log_dir / "audit.jsonl", label="Embedding 完成审计", allow_missing=True
        )
        try:
            os.lstat(target)
        except FileNotFoundError:
            return False
        with layout.open_user_file(target, "rb", label="Embedding 完成审计") as handle:
            for line in handle:
                event = _event(line)
                result = event.get("result")
                claimed_id = result.get("completion_id") if isinstance(result, dict) else None
                if event.get("operation") != "embedding_rebuild":
                    if claimed_id == expected_id:
                        raise ValueError("conflicting completion operation")
                    continue
                if event.get("phase") == "activation_intent" and event.get("operation_id") == operation_id:
                    actual = _identity(
                        event.get("operation_id"), event.get("generation_id"), event.get("manifest_sha256")
                    )
                    if actual != expected:
                        raise ValueError("conflicting activation identity")
                    intent = True
                if event.get("phase") != "completed":
                    if claimed_id == expected_id:
                        raise ValueError("conflicting completion phase")
                    continue
                if not isinstance(result, dict):
                    raise ValueError("unknown completion format")
                if "completion_id" in result:
                    actual = _identity(
                        result.get("operation_id"), result.get("generation_id"), result.get("manifest_sha256")
                    )
                    actual_id = completion_id(**actual)
                    if claimed_id != actual_id:
                        raise ValueError("conflicting completion identity")
                    # The hash names the required identity, but two events
                    # bearing that identity must also agree on retained
                    # publication facts.  Context/timestamp are not facts.
                    facts = {**actual, "previous_generation": result.get("previous_generation")}
                    if claimed_id in seen and seen[claimed_id] != facts:
                        raise ValueError("conflicting duplicate completion")
                    seen[claimed_id] = facts
                    if actual["operation_id"] == operation_id:
                        if actual != expected:
                            raise ValueError("conflicting operation completion")
                        modern = True
                elif result.get("operation_id") == operation_id:
                    if result.get("generation_id") != generation_id:
                        raise ValueError("conflicting legacy generation")
                    if "manifest_sha256" in result and result["manifest_sha256"] != manifest_sha256:
                        raise ValueError("conflicting legacy manifest")
                    legacy = True
    except Exception:
        # Never retain a JSON decoder excerpt, path, OS message or user payload
        # in the public error chain.
        raise _repair() from None
    return modern or (legacy and intent and publication_verified)


def append_completion(
    layout: RuntimeLayout,
    *,
    operation_id: str,
    generation_id: str,
    manifest_sha256: str,
    publication_verified: bool,
    context: Mapping[str, Any],
    secret_values: Iterable[str],
    previous_generation: str | None = None,
) -> bool:
    """Append once under the caller's lease; return whether a new line was added.

    The caller must have just revalidated publication while holding this lease.
    AuditTrace supplies sanitization and append/fsync; no in-memory terminal bit
    substitutes for the durable lookup on a retry or in a fresh process.
    """

    require_active_data_root_writer(layout, owner="runtime_audit")
    if publication_verified is not True:
        raise _repair()
    identity = _identity(operation_id, generation_id, manifest_sha256)
    if inspect_completion(layout, **identity, publication_verified=True):
        return False
    AuditTrace(layout, secret_values=secret_values).append(
        {
            "operation": "embedding_rebuild",
            "phase": "completed",
            "context": context,
            "result": {
                **identity,
                "completion_id": completion_id(**identity),
                "previous_generation": previous_generation,
            },
        }
    )
    return True


__all__ = ["completion_id", "inspect_completion", "append_completion"]
