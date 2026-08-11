"""Deterministic external OpenAI-compatible loopback provider for W3/W4.

This executable is deliberately independent from the PKV product runtime.  It
uses only the Python standard library, binds only to the numeric IPv4 loopback
address, and selects responses from an ordered, versioned script.  Product
requests cannot select a scenario through prompts, models, headers, or hidden
test switches.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import select
import socket
from socketserver import TCPServer
import sys
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence


HARNESS_VERSION = "1.0.0"
CONTRACT_ID = "w3.openai_compatible_loopback.v1"
CONTRACT_SCHEMA = "pkv.w3.loopback.contract.v1"
MANIFEST_SCHEMA = "pkv.w3.loopback.manifest.v1"
SCRIPT_SCHEMA = "pkv.w3.loopback.script.v1"
READY_SCHEMA = "pkv.w3.loopback.ready.v1"
HEALTH_SCHEMA = "pkv.w3.loopback.health.v1"
CONTRACT_RESPONSE_SCHEMA = "pkv.w3.loopback.contract-response.v1"
TELEMETRY_SCHEMA = "pkv.w3.loopback.telemetry.v1"
RESULT_SCHEMA = "pkv.w3.loopback.result.v1"

LOOPBACK_HOST = "127.0.0.1"
CHAT_PATH = "/v1/chat/completions"
MAX_REQUEST_BYTES = 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_SCRIPT_STEPS = 32
MAX_CONTENT_CHUNKS = 10_000
SHUTDOWN_CONTENT = b"shutdown\n"

EXIT_OK = 0
EXIT_CONTRACT_INVALID = 2
EXIT_STARTUP_FAILED = 3
EXIT_EXECUTION_FAILED = 4
EXIT_IDLE_TIMEOUT = 5

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SUPPORTED_SCENARIOS = frozenset({"success", "slow_stop", "provider_error"})
_MESSAGE_ROLES = frozenset({"system", "user", "assistant"})
_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "stream",
        "stream_options",
        "max_tokens",
        "temperature",
    }
)


class HarnessContractError(ValueError):
    """Raised when versioned harness input violates its public contract."""


class HarnessStartupError(RuntimeError):
    """Raised when the isolated runtime cannot start safely."""


class DuplicateKeyError(HarnessContractError):
    """Raised for ambiguous JSON objects."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize evidence material without timestamps or platform formatting."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _reject_duplicate_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def parse_json_bytes(raw: bytes, *, label: str) -> Any:
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise HarnessContractError(f"{label} size is invalid")
    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(text, object_pairs_hook=_reject_duplicate_pairs)
    except HarnessContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessContractError(f"{label} is not strict UTF-8 JSON") from exc


def load_json_file(path: Path, *, label: str) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise HarnessContractError(f"cannot read {label}") from exc
    return parse_json_bytes(raw, label=label)


def _require_exact_keys(
    value: Any,
    expected: set[str] | frozenset[str],
    *,
    label: str,
) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(expected):
        raise HarnessContractError(f"{label} fields are invalid")
    return value


def _require_identifier(value: Any, *, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise HarnessContractError(f"{label} is invalid")
    return value


def _require_nonempty_text(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise HarnessContractError(f"{label} is invalid")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if type(value) is not str or _HEX_SHA256.fullmatch(value) is None:
        raise HarnessContractError(f"{label} is not a lowercase SHA-256")
    return value


def _require_relative_path_text(value: Any, *, label: str) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise HarnessContractError(f"{label} path is invalid")
    candidate = Path(value)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise HarnessContractError(f"{label} path is not a safe relative path")
    return value


def _require_int(
    value: Any,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise HarnessContractError(f"{label} is invalid")
    return value


def _require_finite_number(value: Any, *, label: str) -> int | float:
    if type(value) not in {int, float} or (
        type(value) is float and not math.isfinite(value)
    ):
        raise HarnessContractError(f"{label} is invalid")
    return value


def _contained_regular_file(root: Path, relative: Any, *, label: str) -> Path:
    candidate_rel = Path(_require_relative_path_text(relative, label=label))
    candidate = root.joinpath(candidate_rel)
    cursor = root
    for part in candidate_rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise HarnessContractError(f"{label} path contains a symbolic link")
    try:
        resolved_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise HarnessContractError(
            f"{label} path is unavailable or escapes package"
        ) from exc
    if not resolved.is_file():
        raise HarnessContractError(f"{label} is not a regular file")
    return resolved


@dataclass(frozen=True)
class ExpectedRequest:
    model: str
    message_roles: tuple[str, ...]
    last_user_sha256: str
    max_tokens: int
    temperature: int | float


@dataclass(frozen=True)
class ScriptStep:
    step_id: str
    scenario: str
    expect: ExpectedRequest
    response: Mapping[str, Any]


@dataclass(frozen=True)
class HarnessScript:
    script_id: str
    steps: tuple[ScriptStep, ...]
    sha256: str


@dataclass(frozen=True)
class ManifestBundle:
    manifest_path: Path
    manifest_sha256: str
    manifest: Mapping[str, Any]
    runtime_path: Path
    contract_path: Path
    contract: Mapping[str, Any]
    contract_sha256: str
    script_path: Path
    script: HarnessScript


@dataclass(frozen=True)
class ValidatedRequest:
    canonical_request_sha256: str
    model: str
    message_roles: tuple[str, ...]
    message_count: int
    last_user_sha256: str


def validate_contract(value: Any) -> Mapping[str, Any]:
    contract = _require_exact_keys(
        value,
        {
            "schema_version",
            "contract_id",
            "harness_version",
            "network_policy",
            "endpoints",
            "request_contract",
            "stream_contract",
            "scenario_contract",
            "telemetry_policy",
        },
        label="contract",
    )
    if contract["schema_version"] != CONTRACT_SCHEMA:
        raise HarnessContractError("contract schema is unsupported")
    if contract["contract_id"] != CONTRACT_ID:
        raise HarnessContractError("contract id is unsupported")
    if contract["harness_version"] != HARNESS_VERSION:
        raise HarnessContractError("contract harness version does not match executable")
    network = _require_exact_keys(
        contract["network_policy"],
        {"bind_host", "address_family", "outbound_connections", "dns_resolution"},
        label="network policy",
    )
    if network != {
        "bind_host": LOOPBACK_HOST,
        "address_family": "AF_INET",
        "outbound_connections": "forbidden",
        "dns_resolution": "forbidden",
    }:
        raise HarnessContractError("network policy is not fail-closed")
    endpoints = contract["endpoints"]
    if endpoints != {
        "health": "GET /health",
        "contract": "GET /contract",
        "telemetry": "GET /telemetry",
        "chat_completions": f"POST {CHAT_PATH}",
    }:
        raise HarnessContractError("endpoint contract is invalid")
    return contract


def _validate_expected_request(value: Any) -> ExpectedRequest:
    expected = _require_exact_keys(
        value,
        {"model", "message_roles", "last_user_sha256", "max_tokens", "temperature"},
        label="script request expectation",
    )
    model = _require_nonempty_text(expected["model"], label="expected model")
    roles_raw = expected["message_roles"]
    if (
        type(roles_raw) is not list
        or not roles_raw
        or any(
            type(role) is not str or role not in _MESSAGE_ROLES for role in roles_raw
        )
        or roles_raw[-1] != "user"
    ):
        raise HarnessContractError("expected message roles are invalid")
    return ExpectedRequest(
        model=model,
        message_roles=tuple(roles_raw),
        last_user_sha256=_require_sha256(
            expected["last_user_sha256"],
            label="expected last user hash",
        ),
        max_tokens=_require_int(
            expected["max_tokens"],
            label="expected max_tokens",
            minimum=1,
            maximum=1_000_000,
        ),
        temperature=_require_finite_number(
            expected["temperature"],
            label="expected temperature",
        ),
    )


def _validate_response(scenario: str, value: Any) -> Mapping[str, Any]:
    if scenario == "success":
        response = _require_exact_keys(
            value,
            {"content_chunks", "prompt_tokens", "completion_tokens"},
            label="success response",
        )
        chunks = response["content_chunks"]
        if (
            type(chunks) is not list
            or not chunks
            or len(chunks) > MAX_CONTENT_CHUNKS
            or any(type(chunk) is not str or not chunk for chunk in chunks)
        ):
            raise HarnessContractError("success content chunks are invalid")
        _require_int(
            response["prompt_tokens"],
            label="success prompt_tokens",
            minimum=0,
            maximum=1_000_000_000,
        )
        _require_int(
            response["completion_tokens"],
            label="success completion_tokens",
            minimum=0,
            maximum=1_000_000_000,
        )
    elif scenario == "slow_stop":
        response = _require_exact_keys(
            value,
            {"first_chunk", "continuation_chunk", "interval_ms", "max_chunks"},
            label="slow-stop response",
        )
        _require_nonempty_text(response["first_chunk"], label="slow-stop first chunk")
        _require_nonempty_text(
            response["continuation_chunk"],
            label="slow-stop continuation chunk",
        )
        _require_int(
            response["interval_ms"],
            label="slow-stop interval",
            minimum=10,
            maximum=10_000,
        )
        _require_int(
            response["max_chunks"],
            label="slow-stop chunk limit",
            minimum=2,
            maximum=MAX_CONTENT_CHUNKS,
        )
    elif scenario == "provider_error":
        response = _require_exact_keys(
            value,
            {"status", "error_type", "error_code", "message"},
            label="provider-error response",
        )
        if response["status"] != 503:
            raise HarnessContractError("provider-error status must be 503")
        for field in ("error_type", "error_code", "message"):
            _require_nonempty_text(response[field], label=f"provider-error {field}")
    else:  # pragma: no cover - caller validates this first
        raise HarnessContractError("unsupported scenario")
    return response


def load_script(path: Path) -> HarnessScript:
    raw = path.read_bytes()
    value = parse_json_bytes(raw, label="script")
    script = _require_exact_keys(
        value,
        {"schema_version", "contract_id", "script_id", "steps"},
        label="script",
    )
    if (
        script["schema_version"] != SCRIPT_SCHEMA
        or script["contract_id"] != CONTRACT_ID
    ):
        raise HarnessContractError("script schema or contract is unsupported")
    script_id = _require_identifier(script["script_id"], label="script id")
    steps_raw = script["steps"]
    if type(steps_raw) is not list or not 1 <= len(steps_raw) <= MAX_SCRIPT_STEPS:
        raise HarnessContractError("script step count is invalid")
    steps: list[ScriptStep] = []
    seen_ids: set[str] = set()
    for raw_step in steps_raw:
        step = _require_exact_keys(
            raw_step,
            {"step_id", "scenario", "expect", "response"},
            label="script step",
        )
        step_id = _require_identifier(step["step_id"], label="step id")
        if step_id in seen_ids:
            raise HarnessContractError("script step ids must be unique")
        seen_ids.add(step_id)
        scenario = step["scenario"]
        if type(scenario) is not str or scenario not in _SUPPORTED_SCENARIOS:
            raise HarnessContractError("script scenario is unsupported")
        steps.append(
            ScriptStep(
                step_id=step_id,
                scenario=scenario,
                expect=_validate_expected_request(step["expect"]),
                response=_validate_response(scenario, step["response"]),
            )
        )
    return HarnessScript(
        script_id=script_id, steps=tuple(steps), sha256=sha256_bytes(raw)
    )


def _validate_manifest_document(value: Any) -> Mapping[str, Any]:
    manifest = _require_exact_keys(
        value,
        {
            "schema_version",
            "contract_id",
            "harness_version",
            "distribution",
            "release_payload_membership",
            "runtime",
            "contract",
            "scripts",
            "build",
        },
        label="manifest",
    )
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        raise HarnessContractError("manifest schema is unsupported")
    if manifest["contract_id"] != CONTRACT_ID:
        raise HarnessContractError("manifest contract id is unsupported")
    if manifest["harness_version"] != HARNESS_VERSION:
        raise HarnessContractError("manifest harness version does not match executable")
    if manifest["distribution"] != "e2e-only":
        raise HarnessContractError("manifest distribution must be e2e-only")
    if manifest["release_payload_membership"] != "forbidden":
        raise HarnessContractError(
            "manifest does not forbid release payload membership"
        )
    runtime = _require_exact_keys(
        manifest["runtime"],
        {"kind", "path", "size", "sha256"},
        label="manifest runtime",
    )
    if runtime["kind"] not in {"source", "frozen"}:
        raise HarnessContractError("manifest runtime kind is invalid")
    _require_relative_path_text(runtime["path"], label="runtime")
    _require_int(runtime["size"], label="runtime size", minimum=1, maximum=2**63 - 1)
    _require_sha256(runtime["sha256"], label="runtime hash")
    contract = _require_exact_keys(
        manifest["contract"],
        {"path", "sha256"},
        label="manifest contract",
    )
    _require_relative_path_text(contract["path"], label="contract")
    _require_sha256(contract["sha256"], label="contract hash")
    scripts = manifest["scripts"]
    if type(scripts) is not list or not scripts:
        raise HarnessContractError("manifest scripts are invalid")
    script_ids: set[str] = set()
    script_paths: set[str] = set()
    for raw_script in scripts:
        entry = _require_exact_keys(
            raw_script,
            {"script_id", "path", "sha256"},
            label="manifest script",
        )
        script_id = _require_identifier(entry["script_id"], label="manifest script id")
        script_path = _require_relative_path_text(
            entry["path"],
            label="manifest script",
        )
        if script_id in script_ids or script_path in script_paths:
            raise HarnessContractError("manifest scripts must be unique")
        script_ids.add(script_id)
        script_paths.add(script_path)
        _require_sha256(entry["sha256"], label="manifest script hash")
    build = _require_exact_keys(
        manifest["build"],
        {"source_revision", "build_fingerprint_sha256", "toolchain_lock_sha256"},
        label="manifest build",
    )
    _require_nonempty_text(build["source_revision"], label="source revision")
    _require_sha256(build["build_fingerprint_sha256"], label="build fingerprint")
    _require_sha256(build["toolchain_lock_sha256"], label="toolchain lock hash")
    return manifest


def load_manifest_bundle(
    manifest_path: Path,
    script_path: Path,
    *,
    actual_runtime_path: Path | None = None,
    actual_runtime_kind: str | None = None,
) -> ManifestBundle:
    manifest_path = manifest_path.resolve(strict=True)
    package_root = manifest_path.parent
    manifest_raw = manifest_path.read_bytes()
    manifest = _validate_manifest_document(
        parse_json_bytes(manifest_raw, label="manifest")
    )

    runtime_entry = manifest["runtime"]
    runtime_path = _contained_regular_file(
        package_root,
        runtime_entry["path"],
        label="runtime",
    )
    if runtime_path.stat().st_size != runtime_entry["size"]:
        raise HarnessContractError("runtime size does not match manifest")
    if sha256_file(runtime_path) != runtime_entry["sha256"]:
        raise HarnessContractError("runtime hash does not match manifest")
    if actual_runtime_path is not None and runtime_path != actual_runtime_path.resolve(
        strict=True
    ):
        raise HarnessContractError("manifest runtime is not the running executable")
    if actual_runtime_kind is not None and runtime_entry["kind"] != actual_runtime_kind:
        raise HarnessContractError("manifest runtime kind does not match process")

    contract_entry = manifest["contract"]
    contract_path = _contained_regular_file(
        package_root,
        contract_entry["path"],
        label="contract",
    )
    contract_sha256 = sha256_file(contract_path)
    if contract_sha256 != contract_entry["sha256"]:
        raise HarnessContractError("contract hash does not match manifest")
    contract = validate_contract(load_json_file(contract_path, label="contract"))

    selected_path = script_path.resolve(strict=True)
    selected_entry: Mapping[str, Any] | None = None
    for candidate in manifest["scripts"]:
        candidate_path = _contained_regular_file(
            package_root,
            candidate["path"],
            label="script",
        )
        if candidate_path == selected_path:
            selected_entry = candidate
            break
    if selected_entry is None:
        raise HarnessContractError("selected script is not allowlisted by manifest")
    selected_sha256 = sha256_file(selected_path)
    if selected_sha256 != selected_entry["sha256"]:
        raise HarnessContractError("script hash does not match manifest")
    script = load_script(selected_path)
    if script.script_id != selected_entry["script_id"]:
        raise HarnessContractError("script id does not match manifest")

    return ManifestBundle(
        manifest_path=manifest_path,
        manifest_sha256=sha256_bytes(manifest_raw),
        manifest=manifest,
        runtime_path=runtime_path,
        contract_path=contract_path,
        contract=contract,
        contract_sha256=contract_sha256,
        script_path=selected_path,
        script=script,
    )


def validate_chat_request(
    value: Any,
    *,
    expected: ExpectedRequest,
) -> ValidatedRequest:
    request = _require_exact_keys(value, _REQUEST_FIELDS, label="chat request")
    if request["stream"] is not True:
        raise HarnessContractError("chat request must enable streaming")
    if request["stream_options"] != {"include_usage": True}:
        raise HarnessContractError("chat request must request usage")
    if request["model"] != expected.model:
        raise HarnessContractError("chat request model does not match script")
    if (
        type(request["max_tokens"]) is not int
        or request["max_tokens"] != expected.max_tokens
    ):
        raise HarnessContractError("chat request max_tokens does not match script")
    temperature = request["temperature"]
    _require_finite_number(temperature, label="chat request temperature")
    if temperature != expected.temperature:
        raise HarnessContractError("chat request temperature does not match script")

    raw_messages = request["messages"]
    if type(raw_messages) is not list or not raw_messages:
        raise HarnessContractError("chat request messages are invalid")
    roles: list[str] = []
    for message in raw_messages:
        projected = _require_exact_keys(
            message,
            {"role", "content"},
            label="chat message",
        )
        role = projected["role"]
        content = projected["content"]
        if (
            type(role) is not str
            or role not in _MESSAGE_ROLES
            or type(content) is not str
        ):
            raise HarnessContractError("chat message role or content is invalid")
        roles.append(role)
    if tuple(roles) != expected.message_roles:
        raise HarnessContractError("chat message roles do not match script")
    if raw_messages[-1]["role"] != "user":
        raise HarnessContractError("chat request must end with a user message")
    last_user_sha256 = sha256_bytes(raw_messages[-1]["content"].encode("utf-8"))
    if last_user_sha256 != expected.last_user_sha256:
        raise HarnessContractError("last user content does not match script")

    return ValidatedRequest(
        canonical_request_sha256=sha256_bytes(canonical_json_bytes(request)),
        model=request["model"],
        message_roles=tuple(roles),
        message_count=len(raw_messages),
        last_user_sha256=last_user_sha256,
    )


class HarnessState:
    """Thread-safe ordered script owner and append-only sanitized telemetry."""

    def __init__(self, script: HarnessScript, telemetry_handle) -> None:
        self.script = script
        self._telemetry_handle = telemetry_handle
        self._lock = threading.RLock()
        self._index = 0
        self._active_step: ScriptStep | None = None
        self._sequence = 0
        self._records: list[dict[str, Any]] = []
        self._violations: list[str] = []
        self._idle_timed_out = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status = (
                "failed"
                if self._violations
                else (
                    "complete"
                    if self._index == len(self.script.steps)
                    and self._active_step is None
                    else ("serving" if self._active_step is not None else "ready")
                )
            )
            return {
                "status": status,
                "script_id": self.script.script_id,
                "script_sha256": self.script.sha256,
                "completed_steps": self._index,
                "total_steps": len(self.script.steps),
                "active_step_id": (
                    self._active_step.step_id if self._active_step is not None else None
                ),
                "violations": list(self._violations),
                "records": [dict(record) for record in self._records],
            }

    @property
    def idle_timed_out(self) -> bool:
        with self._lock:
            return self._idle_timed_out

    def mark_idle_timeout(self) -> None:
        with self._lock:
            self._idle_timed_out = True
            self._record_locked(
                "contract_violation",
                code="idle_timeout",
                step_id=(self._active_step.step_id if self._active_step else None),
            )
            self._violations.append("idle_timeout")

    def reject(self, code: str, *, step_id: str | None = None) -> None:
        with self._lock:
            self._violations.append(code)
            self._record_locked("contract_violation", code=code, step_id=step_id)

    def begin(self, request: Any) -> tuple[ScriptStep, ValidatedRequest] | None:
        with self._lock:
            if self._active_step is not None:
                self.reject("harness_busy", step_id=self._active_step.step_id)
                return None
            if self._index >= len(self.script.steps):
                self.reject("script_exhausted")
                return None
            step = self.script.steps[self._index]
            try:
                validated = validate_chat_request(request, expected=step.expect)
            except HarnessContractError:
                self.reject("request_contract_invalid", step_id=step.step_id)
                return None
            self._active_step = step
            self._record_locked(
                "request_accepted",
                step_id=step.step_id,
                scenario=step.scenario,
                canonical_request_sha256=validated.canonical_request_sha256,
                model=validated.model,
                message_roles=list(validated.message_roles),
                message_count=validated.message_count,
                last_user_sha256=validated.last_user_sha256,
                authorization_present=True,
            )
            return step, validated

    def complete(self, step: ScriptStep, outcome: str, **details: Any) -> None:
        with self._lock:
            if self._active_step is not step:
                self.reject("step_ownership_conflict", step_id=step.step_id)
                return
            if outcome not in {
                "stream_completed",
                "client_cancelled",
                "provider_error",
            }:
                self.reject("step_outcome_invalid", step_id=step.step_id)
                self._active_step = None
                self._index += 1
                return
            expected_outcome = {
                "success": "stream_completed",
                "slow_stop": "client_cancelled",
                "provider_error": "provider_error",
            }[step.scenario]
            if outcome != expected_outcome:
                self._violations.append("scenario_outcome_mismatch")
            self._record_locked(
                outcome,
                step_id=step.step_id,
                scenario=step.scenario,
                **details,
            )
            self._active_step = None
            self._index += 1

    def fail_active(self, step: ScriptStep, code: str, **details: Any) -> None:
        with self._lock:
            if self._active_step is not step:
                self.reject("step_ownership_conflict", step_id=step.step_id)
                return
            self._violations.append(code)
            self._record_locked(
                "contract_violation",
                code=code,
                step_id=step.step_id,
                scenario=step.scenario,
                **details,
            )
            self._active_step = None
            self._index += 1

    def _record_locked(self, event: str, **fields: Any) -> None:
        self._sequence += 1
        record = {"sequence": self._sequence, "event": event, **fields}
        self._records.append(record)
        self._telemetry_handle.write(canonical_json_bytes(record) + b"\n")
        self._telemetry_handle.flush()
        os.fsync(self._telemetry_handle.fileno())


class LoopbackServer(ThreadingHTTPServer):
    address_family = socket.AF_INET
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        """Bind without ``HTTPServer``'s reverse-DNS lookup."""

        TCPServer.server_bind(self)
        self.server_name = LOOPBACK_HOST
        self.server_port = self.server_address[1]

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_type,
        *,
        bundle: ManifestBundle,
        state: HarnessState,
        harness_sha256: str,
        frozen: bool,
    ) -> None:
        self.bundle = bundle
        self.state = state
        self.harness_sha256 = harness_sha256
        self.frozen = frozen
        super().__init__(server_address, handler_type, bind_and_activate=True)


class LoopbackRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PKVLoopback"
    sys_version = ""

    @property
    def harness_server(self) -> LoopbackServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def setup(self) -> None:
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._valid_host_header():
            self._send_error_json(400, "host_header_invalid")
            return
        if self.path == "/health":
            self._send_json(200, self._health_payload())
        elif self.path == "/contract":
            bundle = self.harness_server.bundle
            self._send_json(
                200,
                {
                    "schema_version": CONTRACT_RESPONSE_SCHEMA,
                    "contract_id": CONTRACT_ID,
                    "contract_sha256": bundle.contract_sha256,
                    "manifest_sha256": bundle.manifest_sha256,
                    "harness_sha256": self.harness_server.harness_sha256,
                    "frozen": self.harness_server.frozen,
                    "contract": bundle.contract,
                },
            )
        elif self.path == "/telemetry":
            self._send_json(
                200,
                {
                    "schema_version": TELEMETRY_SCHEMA,
                    "contract_id": CONTRACT_ID,
                    **self.harness_server.state.snapshot(),
                },
            )
        else:
            self._send_error_json(404, "route_not_found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._valid_host_header():
            self.harness_server.state.reject("host_header_invalid")
            self._send_error_json(400, "host_header_invalid")
            return
        if self.path != CHAT_PATH:
            self.harness_server.state.reject("route_not_found")
            self._send_error_json(404, "route_not_found")
            return
        if not self._valid_authorization():
            self.harness_server.state.reject("authorization_invalid")
            self._send_error_json(401, "authorization_invalid")
            return
        try:
            request = self._read_json_request()
        except HarnessContractError:
            self.harness_server.state.reject("request_body_invalid")
            self._send_error_json(400, "request_body_invalid")
            return
        accepted = self.harness_server.state.begin(request)
        if accepted is None:
            snapshot = self.harness_server.state.snapshot()
            code = snapshot["violations"][-1]
            status = 409 if code in {"harness_busy", "script_exhausted"} else 400
            self._send_error_json(status, code)
            return
        step, _ = accepted
        if step.scenario == "success":
            self._serve_success(step)
        elif step.scenario == "slow_stop":
            self._serve_slow_stop(step)
        else:
            self._serve_provider_error(step)

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self.harness_server.state.reject("method_not_allowed")
        self._send_error_json(
            405, "method_not_allowed", extra_headers={"Allow": "GET, POST"}
        )

    def _valid_host_header(self) -> bool:
        expected = f"{LOOPBACK_HOST}:{self.harness_server.server_port}"
        return self.headers.get("Host") == expected

    def _valid_authorization(self) -> bool:
        authorization = self.headers.get("Authorization")
        return (
            type(authorization) is str
            and authorization.startswith("Bearer ")
            and bool(authorization[7:])
            and authorization == authorization.strip()
        )

    def _read_json_request(self) -> Any:
        if self.headers.get("Transfer-Encoding") is not None:
            raise HarnessContractError("chunked requests are unsupported")
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            raise HarnessContractError("request content type is invalid")
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "", 10)
        except ValueError as exc:
            raise HarnessContractError("request content length is invalid") from exc
        if not 1 <= content_length <= MAX_REQUEST_BYTES:
            raise HarnessContractError("request content length is invalid")
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            raise HarnessContractError("request body is truncated")
        return parse_json_bytes(raw, label="chat request")

    def _health_payload(self) -> dict[str, Any]:
        return {
            "schema_version": HEALTH_SCHEMA,
            "contract_id": CONTRACT_ID,
            "host": LOOPBACK_HOST,
            "port": self.harness_server.server_port,
            "frozen": self.harness_server.frozen,
            **{
                key: value
                for key, value in self.harness_server.state.snapshot().items()
                if key != "records"
            },
        }

    def _serve_success(self, step: ScriptStep) -> None:
        content_chunks = step.response["content_chunks"]
        chunk_count = 0
        try:
            self._begin_sse()
            self._write_sse_json(self._chat_chunk(step, delta={"role": "assistant"}))
            for content in content_chunks:
                self._write_sse_json(self._chat_chunk(step, delta={"content": content}))
                chunk_count += 1
            self._write_sse_json(self._chat_chunk(step, delta={}, finish_reason="stop"))
            prompt_tokens = step.response["prompt_tokens"]
            completion_tokens = step.response["completion_tokens"]
            self._write_sse_json(
                {
                    "id": f"chatcmpl-{step.step_id}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": step.expect.model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
            )
            self._write_sse_data(b"[DONE]")
            self._finish_chunked_response()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            self.harness_server.state.fail_active(
                step,
                "success_stream_disconnected",
                response_status=200,
                content_chunks_sent=chunk_count,
                finish_sent=False,
                usage_sent=False,
                done_sent=False,
                client_disconnected=True,
            )
            return
        self.harness_server.state.complete(
            step,
            "stream_completed",
            response_status=200,
            content_chunks_sent=chunk_count,
            finish_sent=True,
            usage_sent=True,
            done_sent=True,
            client_disconnected=False,
        )

    def _serve_slow_stop(self, step: ScriptStep) -> None:
        sent = 0
        try:
            self._begin_sse()
            self._write_sse_json(self._chat_chunk(step, delta={"role": "assistant"}))
            self._write_sse_json(
                self._chat_chunk(step, delta={"content": step.response["first_chunk"]})
            )
            sent = 1
            interval = step.response["interval_ms"] / 1000.0
            for _ in range(1, step.response["max_chunks"]):
                time.sleep(interval)
                if self._peer_disconnected():
                    self.harness_server.state.complete(
                        step,
                        "client_cancelled",
                        response_status=200,
                        content_chunks_sent=sent,
                        finish_sent=False,
                        usage_sent=False,
                        done_sent=False,
                        client_disconnected=True,
                    )
                    return
                self._write_sse_json(
                    self._chat_chunk(
                        step,
                        delta={"content": step.response["continuation_chunk"]},
                    )
                )
                sent += 1
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            self.harness_server.state.complete(
                step,
                "client_cancelled",
                response_status=200,
                content_chunks_sent=sent,
                finish_sent=False,
                usage_sent=False,
                done_sent=False,
                client_disconnected=True,
            )
            return
        self.harness_server.state.fail_active(
            step,
            "client_did_not_cancel",
            response_status=200,
            content_chunks_sent=sent,
            finish_sent=False,
            usage_sent=False,
            done_sent=False,
            client_disconnected=False,
        )

    def _serve_provider_error(self, step: ScriptStep) -> None:
        response = step.response
        try:
            self._send_json(
                503,
                {
                    "error": {
                        "message": response["message"],
                        "type": response["error_type"],
                        "param": None,
                        "code": response["error_code"],
                    }
                },
                extra_headers={"Retry-After": "0"},
            )
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            self.harness_server.state.fail_active(
                step,
                "provider_error_delivery_failed",
                response_status=503,
                client_disconnected=True,
            )
            return
        self.harness_server.state.complete(
            step,
            "provider_error",
            response_status=503,
            content_chunks_sent=0,
            finish_sent=False,
            usage_sent=False,
            done_sent=False,
            client_disconnected=False,
        )

    @staticmethod
    def _chat_chunk(
        step: ScriptStep,
        *,
        delta: Mapping[str, Any],
        finish_reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{step.step_id}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": step.expect.model,
            "choices": [
                {
                    "index": 0,
                    "delta": dict(delta),
                    "finish_reason": finish_reason,
                }
            ],
        }

    def _begin_sse(self) -> None:
        self.send_response_only(200, "OK")
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

    def _write_sse_json(self, value: Mapping[str, Any]) -> None:
        self._write_sse_data(canonical_json_bytes(value))

    def _write_sse_data(self, value: bytes) -> None:
        frame = b"data: " + value + b"\n\n"
        self.wfile.write(f"{len(frame):x}\r\n".encode("ascii"))
        self.wfile.write(frame)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _finish_chunked_response(self) -> None:
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _peer_disconnected(self) -> bool:
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (BlockingIOError, InterruptedError):
            return False
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            return True

    def _send_error_json(
        self,
        status: int,
        code: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._send_json(
            status,
            {
                "error": {
                    "message": "loopback harness request rejected",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": code,
                }
            },
            extra_headers=extra_headers,
        )

    def _send_json(
        self,
        status: int,
        value: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = canonical_json_bytes(value)
        self.send_response_only(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, item in (extra_headers or {}).items():
            self.send_header(name, item)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def _runtime_identity() -> tuple[str, Path]:
    if bool(getattr(sys, "frozen", False)):
        return "frozen", Path(sys.executable)
    return "source", Path(__file__)


def _validate_state_dir(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HarnessStartupError("state directory does not exist") from exc
    if path.is_symlink() or not resolved.is_dir():
        raise HarnessStartupError("state directory is unsafe")
    if any(resolved.iterdir()):
        raise HarnessStartupError("state directory must be empty")
    return resolved


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(value) + b"\n"
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _ready_payload(
    server: LoopbackServer,
    bundle: ManifestBundle,
) -> dict[str, Any]:
    port = server.server_port
    origin = f"http://{LOOPBACK_HOST}:{port}"
    return {
        "schema_version": READY_SCHEMA,
        "contract_id": CONTRACT_ID,
        "harness_version": HARNESS_VERSION,
        "pid": os.getpid(),
        "host": LOOPBACK_HOST,
        "port": port,
        "base_url": f"{origin}/v1",
        "health_url": f"{origin}/health",
        "contract_url": f"{origin}/contract",
        "telemetry_url": f"{origin}/telemetry",
        "frozen": server.frozen,
        "harness_sha256": server.harness_sha256,
        "manifest_sha256": bundle.manifest_sha256,
        "contract_sha256": bundle.contract_sha256,
        "script_id": bundle.script.script_id,
        "script_sha256": bundle.script.sha256,
    }


def _result_payload(
    *,
    state: HarnessState,
    bundle: ManifestBundle,
    harness_sha256: str,
    telemetry_sha256: str,
    frozen: bool,
) -> dict[str, Any]:
    snapshot = state.snapshot()
    passed = (
        snapshot["status"] == "complete"
        and snapshot["completed_steps"] == snapshot["total_steps"]
        and not snapshot["violations"]
    )
    return {
        "schema_version": RESULT_SCHEMA,
        "contract_id": CONTRACT_ID,
        "result": "passed" if passed else "failed",
        "frozen": frozen,
        "harness_sha256": harness_sha256,
        "manifest_sha256": bundle.manifest_sha256,
        "contract_sha256": bundle.contract_sha256,
        "script_id": bundle.script.script_id,
        "script_sha256": bundle.script.sha256,
        "telemetry_sha256": telemetry_sha256,
        "completed_steps": snapshot["completed_steps"],
        "total_steps": snapshot["total_steps"],
        "violations": snapshot["violations"],
    }


def run_server(
    *,
    bundle: ManifestBundle,
    state_dir: Path,
    port: int,
    idle_timeout_seconds: float,
) -> int:
    state_dir = _validate_state_dir(state_dir)
    telemetry_path = state_dir / "telemetry.ndjson"
    ready_path = state_dir / "ready.json"
    shutdown_path = state_dir / "shutdown.request"
    result_path = state_dir / "result.json"

    telemetry_handle = telemetry_path.open("xb")
    runtime_kind, runtime_path = _runtime_identity()
    harness_sha256 = sha256_file(runtime_path)
    frozen = runtime_kind == "frozen"
    state = HarnessState(bundle.script, telemetry_handle)
    server: LoopbackServer | None = None
    watcher: threading.Thread | None = None
    try:
        server = LoopbackServer(
            (LOOPBACK_HOST, port),
            LoopbackRequestHandler,
            bundle=bundle,
            state=state,
            harness_sha256=harness_sha256,
            frozen=frozen,
        )
        atomic_write_json(ready_path, _ready_payload(server, bundle))
        started = time.monotonic()

        def watch_shutdown() -> None:
            while True:
                if shutdown_path.exists():
                    try:
                        if (
                            shutdown_path.is_symlink()
                            or shutdown_path.read_bytes() != SHUTDOWN_CONTENT
                        ):
                            state.reject("shutdown_request_invalid")
                    except OSError:
                        state.reject("shutdown_request_unreadable")
                    server.shutdown()
                    return
                if time.monotonic() - started >= idle_timeout_seconds:
                    state.mark_idle_timeout()
                    server.shutdown()
                    return
                time.sleep(0.05)

        watcher = threading.Thread(
            target=watch_shutdown,
            name="pkv-loopback-shutdown-watcher",
            daemon=True,
        )
        watcher.start()
        server.serve_forever(poll_interval=0.05)
    finally:
        if server is not None:
            server.server_close()
        telemetry_handle.flush()
        os.fsync(telemetry_handle.fileno())
        telemetry_handle.close()
        telemetry_sha256 = sha256_file(telemetry_path)
        result = _result_payload(
            state=state,
            bundle=bundle,
            harness_sha256=harness_sha256,
            telemetry_sha256=telemetry_sha256,
            frozen=frozen,
        )
        atomic_write_json(result_path, result)

    if state.idle_timed_out:
        return EXIT_IDLE_TIMEOUT
    return EXIT_OK if result["result"] == "passed" else EXIT_EXECUTION_FAILED


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pkv-loopback-provider",
        description="Deterministic numeric-loopback OpenAI-compatible E2E provider",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser("version", help="print stable harness identity")
    version.add_argument("--json", action="store_true", required=True)

    verify = subparsers.add_parser(
        "verify", help="verify an allowlisted harness package"
    )
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--script", type=Path, required=True)

    serve = subparsers.add_parser("serve", help="serve one ordered scenario script")
    serve.add_argument("--manifest", type=Path, required=True)
    serve.add_argument("--script", type=Path, required=True)
    serve.add_argument("--state-dir", type=Path, required=True)
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--idle-timeout-seconds", type=float, default=120.0)
    return parser


def _load_cli_bundle(args: argparse.Namespace) -> ManifestBundle:
    runtime_kind, runtime_path = _runtime_identity()
    return load_manifest_bundle(
        args.manifest,
        args.script,
        actual_runtime_path=runtime_path,
        actual_runtime_kind=runtime_kind,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        runtime_kind, runtime_path = _runtime_identity()
        print(
            canonical_json_bytes(
                {
                    "contract_id": CONTRACT_ID,
                    "harness_version": HARNESS_VERSION,
                    "runtime_kind": runtime_kind,
                    "runtime_sha256": sha256_file(runtime_path),
                }
            ).decode("utf-8")
        )
        return EXIT_OK

    try:
        bundle = _load_cli_bundle(args)
        if args.command == "verify":
            print(
                canonical_json_bytes(
                    {
                        "contract_id": CONTRACT_ID,
                        "result": "verified",
                        "runtime_kind": bundle.manifest["runtime"]["kind"],
                        "manifest_sha256": bundle.manifest_sha256,
                        "contract_sha256": bundle.contract_sha256,
                        "script_id": bundle.script.script_id,
                        "script_sha256": bundle.script.sha256,
                    }
                ).decode("utf-8")
            )
            return EXIT_OK
        if type(args.port) is not int or not 0 <= args.port <= 65_535:
            raise HarnessContractError("port must be in [0, 65535]")
        if (
            type(args.idle_timeout_seconds) not in {int, float}
            or not math.isfinite(args.idle_timeout_seconds)
            or not 1.0 <= args.idle_timeout_seconds <= 3600.0
        ):
            raise HarnessContractError("idle timeout must be in [1, 3600]")
        return run_server(
            bundle=bundle,
            state_dir=args.state_dir,
            port=args.port,
            idle_timeout_seconds=float(args.idle_timeout_seconds),
        )
    except HarnessContractError as exc:
        print(
            canonical_json_bytes(
                {
                    "contract_id": CONTRACT_ID,
                    "result": "rejected",
                    "error": type(exc).__name__,
                }
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return EXIT_CONTRACT_INVALID
    except (HarnessStartupError, OSError) as exc:
        print(
            canonical_json_bytes(
                {
                    "contract_id": CONTRACT_ID,
                    "result": "startup_failed",
                    "error": type(exc).__name__,
                }
            ).decode("utf-8"),
            file=sys.stderr,
        )
        return EXIT_STARTUP_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
