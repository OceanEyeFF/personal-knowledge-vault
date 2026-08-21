"""A separate, local-only audit trace channel for reproducible PKV operations.

Normal application logging is deliberately content-redacted.  This module is
the narrow exception: it can retain complete article and Prompt payloads for a
local operator, but it *always* removes credential values before persistence.
It does not configure a logger or write to stdout/stderr, so it cannot weaken
the existing operational-log or MCP-stdio contracts.

Integration seam: the Application/Kernel or embedding-rebuild mutation boundary
creates one ``AuditTrace`` from its captured ``RuntimeLayout`` and uses
``AuditTrace.operation(...)`` only after its writer lease has been acquired.
It supplies the captured configuration generation/root identity itself, so a
request cannot be logged under a mixed configuration generation.  Provider
request tracing remains opt-in at that boundary; this module never constructs a
Provider or reads configuration by itself.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from src.runtime.errors import PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.utils.config import redact_url_credentials


AUDIT_SCHEMA_VERSION = 1
REDACTED_VALUE = "[REDACTED]"
_AUDIT_FILE_NAME = "audit.jsonl"

# Keep the field-name rule as conservative as normal display redaction.  It is
# deliberately applied at every nesting depth, not just to provider settings.
_SENSITIVE_FIELD_MARKERS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "auth_token",
        "authorization",
        "basic_auth",
        "bearer",
        "bearer_token",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "jwt",
        "key",
        "oauth_token",
        "pass",
        "passcode",
        "passphrase",
        "passwd",
        "password",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "session_key",
        "session_token",
        "sid",
        "signature",
        "subscription_key",
        "token",
        "x_api_key",
        "x_auth_token",
    }
)
_URL_PATTERN = re.compile(r"(?:(?:https?|wss?)://|//)[^\s<>\"'`]+", re.IGNORECASE)
_INLINE_CREDENTIAL_PATTERN = re.compile(
    r"(?P<name>\b(?:access[\s_-]*token|api[\s_-]*key|apikey|authorization|"
    r"cookie|client[\s_-]*secret|password|passwd|passphrase|private[\s_-]*key|"
    r"refresh[\s_-]*token|secret|session[\s_-]*token|token|x[\s_-]*api[\s_-]*key)\b)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>(?:(?:bearer|basic|token)\s+)?[^\s,;#&]+)",
    re.IGNORECASE,
)
_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,127}\Z")
_APPEND_LOCK = RLock()


class AuditTraceError(RuntimeError):
    """A generic, secret-safe audit failure.

    The original exception is intentionally never exposed through this error:
    callers must not accidentally reflect a rejected payload or a filesystem
    path into their normal logs/protocol response.
    """

    def __init__(self) -> None:
        super().__init__("审计追踪无法安全写入")


class AuditOperation:
    """One explicit, immutable audit operation timeline.

    A caller creates this object through :meth:`AuditTrace.operation`; it emits
    a ``started`` record immediately and then exactly one terminal record.  The
    request context is sanitized and captured at start time, so a reload or a
    later mutation of the caller's input mapping cannot make one operation's
    audit trail describe a different configuration or payload.

    The class intentionally never serializes an exception message.  A caller
    may use :meth:`fail_runtime_error` for a typed ``PKVRuntimeError``; its
    stable code/stage/recoverability are retained while its potentially unsafe
    text remains out of the trace.  Unknown exceptions become the generic
    ``operation_failed`` outcome.

    This is a recording primitive, not a writer lease.  Product mutation
    boundaries must start it only after they own the data-root write lease;
    read-only inspection must not create it merely to report status.
    """

    def __init__(
        self,
        trace: "AuditTrace",
        *,
        operation: str,
        context: Mapping[str, Any],
    ) -> None:
        self._trace = trace
        self._operation = _require_safe_identifier(operation, label="审计操作")
        # Capture a fully-safe, JSON-compatible copy once.  In particular this
        # avoids reporting a post-reload config graph or a caller-mutated entry
        # body in the terminal event.
        self._context = trace._sanitize_mapping(context, seen=set())
        self._terminal = False
        self._trace.append(self._event("started"))

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def terminal(self) -> bool:
        return self._terminal

    def complete(self, result: Mapping[str, Any] | None = None) -> None:
        """Append the one successful terminal event.

        ``result`` is optional because a state-changing operation may have no
        public result beyond its durable completion.  It is still sanitized by
        the trace before persistence.
        """

        payload = self._event("completed")
        if result is not None:
            payload["result"] = result
        self._append_terminal(payload)

    def fail(
        self,
        *,
        code: str = "operation_failed",
        stage: str | None = None,
        recoverable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a secret-safe failed terminal event.

        The event accepts only a stable identifier and optional stable stage;
        the exception message deliberately has no slot.  ``details`` is for
        explicit structured operation facts (for example a plan id), not an
        exception dump.
        """

        failure: dict[str, Any] = {
            "code": _require_safe_identifier(code, label="审计失败码"),
            "recoverable": bool(recoverable),
        }
        if stage is not None:
            failure["stage"] = _require_safe_identifier(stage, label="审计阶段")
        payload = self._event("failed")
        payload["failure"] = failure
        if details is not None:
            payload["details"] = details
        self._append_terminal(payload)

    def fail_runtime_error(
        self,
        error: PKVRuntimeError,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Record only stable metadata from a typed runtime error."""

        if not isinstance(error, PKVRuntimeError):
            raise TypeError("error 必须是 PKVRuntimeError")
        self.fail(
            code=error.code.value,
            stage=error.stage,
            recoverable=error.recoverable,
            details=details,
        )

    def mark_completion_pending_after_commit(self) -> None:
        """Prevent a false rollback-like outcome after an irreversible commit.

        This narrow escape hatch is for a mutation boundary that has already
        durably committed its own state *and* recorded an ``activation_intent``
        audit event, but whose final ``completed`` append failed (for example a
        full disk).  No audit record is fabricated: the caller must return an
        explicit reconciliation warning instead.  Marking the timeline terminal
        stops the context manager from retrying a second terminal append and
        masking that committed outcome as an uncommitted failure.
        """

        if self._terminal:
            raise RuntimeError("审计操作已经结束")
        self._terminal = True

    def _event(self, phase: str) -> dict[str, Any]:
        return {
            "operation": self._operation,
            "phase": phase,
            "context": self._context,
        }

    def _append_terminal(self, payload: Mapping[str, Any]) -> None:
        if self._terminal:
            raise RuntimeError("审计操作已经结束")
        # Set terminal only after the append succeeds: callers may safely
        # surface a generic AuditTraceError and retry their audit write without
        # accidentally recording two different terminal states.
        self._trace.append(payload)
        self._terminal = True


def _normalize_field_name(value: str) -> str:
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    return re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()


def _is_sensitive_field_name(value: str) -> bool:
    normalized = _normalize_field_name(value)
    return any(
        normalized == marker
        or normalized.startswith(f"{marker}_")
        or normalized.endswith(f"_{marker}")
        or f"_{marker}_" in normalized
        for marker in _SENSITIVE_FIELD_MARKERS
    )


def _require_safe_identifier(value: str, *, label: str) -> str:
    """Accept only an intentionally boring, non-secret protocol identifier."""

    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise AuditTraceError()
    return value


def _redact_urls(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        return redact_url_credentials(candidate) or candidate

    return _URL_PATTERN.sub(replace, value)


def _redact_inline_credentials(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return f"{match.group('name')}{match.group('separator')}{REDACTED_VALUE}"

    return _INLINE_CREDENTIAL_PATTERN.sub(replace, value)


class AuditTrace:
    """Append structured local audit events through a trusted ``RuntimeLayout``.

    ``secret_values`` is deliberately required, even when it is ``()``.  It
    supplies configured credential values that can appear in otherwise
    ordinary text (for example a pasted Prompt), making an integration choose
    its secret collection explicitly.  The future Application/Kernel seam
    must pass all configured Provider secrets here.
    The writer additionally redacts sensitive mapping keys, conventional
    ``name=value`` credential fragments, and credential-bearing URLs.

    Records are one UTF-8 JSON object per physical line at
    ``<layout.log_dir>/audit.jsonl``.  A process-local lock prevents interleave
    among trace writers in this process; R3's cross-process lease/log policy
    will own multi-process ordering without changing this file format.
    """

    def __init__(
        self,
        layout: RuntimeLayout,
        *,
        secret_values: Iterable[str],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(layout, RuntimeLayout):
            raise TypeError("layout 必须是 RuntimeLayout")
        self._layout = layout
        self._secret_values = self._normalize_secret_values(secret_values)
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def path(self) -> Path:
        """The only audit destination, derived from the declared runtime layout."""

        return self._layout.log_dir / _AUDIT_FILE_NAME

    def append(self, event: Mapping[str, Any]) -> Path:
        """Sanitize and durably append one event, or fail without reflection.

        The complete, sanitized record is prepared before the output file is
        opened.  Unsupported/cyclic values and all I/O failures become the
        generic ``AuditTraceError`` rather than exposing the original value.
        """

        try:
            sanitized_event = self._sanitize_mapping(event, seen=set())
            timestamp = self._timestamp()
            record = {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "recorded_at": timestamp,
                "event": sanitized_event,
            }
            line = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                + b"\n"
            )
            # All directory creation and leaf opening remain behind the
            # RuntimeLayout containment/link contract.
            self._layout.ensure_user_directories()
            target = self._layout.writable_user_path(self.path, label="审计追踪日志")
            with _APPEND_LOCK:
                with self._layout.open_user_file(target, "ab", label="审计追踪日志") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            return target
        except AuditTraceError:
            raise
        except Exception:
            raise AuditTraceError() from None

    @contextmanager
    def operation(
        self,
        operation: str,
        *,
        context: Mapping[str, Any],
    ) -> Iterator[AuditOperation]:
        """Record a complete operation lifecycle without reflecting exceptions.

        The body may call ``complete`` or ``fail_runtime_error`` explicitly.  If
        it returns without a terminal call, the operation is recorded as a
        successful completion with no result.  If it raises, the generic
        ``operation_failed`` outcome is appended and the original exception is
        re-raised unchanged; its text is never read or persisted here.

        Merely constructing ``AuditTrace`` or inspecting a runtime must not use
        this method: it appends the first record and therefore deliberately
        creates the local audit file.
        """

        timeline = AuditOperation(self, operation=operation, context=context)
        try:
            yield timeline
        except BaseException:
            if not timeline.terminal:
                # Preserve the business exception.  A disk-full/permission
                # failure while attempting to record an already-failed
                # operation must not replace that operation's own outcome (or
                # cause its potentially unsafe message to be reflected by an
                # adapter).  Successful operations still fail closed if their
                # mandatory terminal audit append cannot be persisted.
                try:
                    timeline.fail()
                except AuditTraceError:
                    pass
            raise
        else:
            if not timeline.terminal:
                timeline.complete()

    def _timestamp(self) -> str:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AuditTraceError()
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_secret_values(values: Iterable[str]) -> tuple[str, ...]:
        try:
            normalized = {value for value in values if value}
        except Exception:
            raise AuditTraceError() from None
        if not all(isinstance(value, str) for value in normalized):
            raise AuditTraceError()
        # Longest-first avoids retaining a suffix when two configured values
        # overlap.  Empty strings are ignored so every article is never erased.
        return tuple(sorted(normalized, key=len, reverse=True))

    def _sanitize_mapping(
        self,
        value: Mapping[str, Any],
        *,
        seen: set[int],
    ) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise AuditTraceError()
        identity = id(value)
        if identity in seen:
            raise AuditTraceError()
        seen.add(identity)
        try:
            sanitized: dict[str, Any] = {}
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise AuditTraceError()
                if _is_sensitive_field_name(key):
                    sanitized[key] = REDACTED_VALUE
                else:
                    sanitized[key] = self._sanitize_value(nested, seen=seen)
            return sanitized
        finally:
            seen.remove(identity)

    def _sanitize_value(self, value: Any, *, seen: set[int]) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise AuditTraceError()
            return value
        if isinstance(value, str):
            return self._redact_text(value)
        if isinstance(value, Mapping):
            return self._sanitize_mapping(value, seen=seen)
        if isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen:
                raise AuditTraceError()
            seen.add(identity)
            try:
                return [self._sanitize_value(item, seen=seen) for item in value]
            finally:
                seen.remove(identity)
        # Do not call str()/repr(): a custom object may render a Provider key.
        raise AuditTraceError()

    def _redact_text(self, value: str) -> str:
        redacted = value
        for secret in self._secret_values:
            redacted = redacted.replace(secret, REDACTED_VALUE)
        return _redact_inline_credentials(_redact_urls(redacted))


__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "REDACTED_VALUE",
    "AuditOperation",
    "AuditTrace",
    "AuditTraceError",
]
