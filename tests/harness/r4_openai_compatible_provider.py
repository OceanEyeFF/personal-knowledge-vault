#!/usr/bin/env python3
"""Deterministic, process-external OpenAI-compatible provider for R4 tests.

This executable is test infrastructure, not a provider implementation used by
PKV.  It binds an ephemeral IPv4 loopback port, consumes a versioned ordered
script, and never chooses behaviour from a request header, model, prompt, or
credential.

Script contract (all object fields are exact; unknown fields are rejected)::

    {
      "schema_version": "pkv.r4.openai-compatible-script.v1",
      "scenario_id": "cli-success",
      "chat_model": "pkv-r4-chat-test-v1",
      "embedding_model": "pkv-r4-embedding-test-v1",
      "embedding_dimension": 4,
      "barrier_timeout_seconds": 30,
      "steps": [
        {
          "step_id": "summary",
          "endpoint": "/v1/chat/completions",
          "barrier": false,
          "response": {
            "kind": "success",
            "content": "deterministic summary",
            "usage": {"prompt_tokens": 11, "completion_tokens": 3}
          }
        },
        {
          "step_id": "embedding",
          "endpoint": "/v1/embeddings",
          "barrier": true,
          "response": {
            "kind": "success",
            "vectors": [[1.0, 0.0, 0.0, 0.0]],
            "usage": {"prompt_tokens": 7}
          }
        }
      ]
    }

An error response replaces the success-specific fields with::

    "response": {
      "kind": "error",
      "status": 503,
      "code": "controlled_provider_failure"
    }

For a barrier step the controller waits for the ``request_waiting`` JSONL
event on stdout, then writes ``CONTINUE <step_id>`` to stdin.  ``SHUTDOWN`` is
the only other control command.  Waiting is bounded by the script timeout.

The state directory receives atomic ``ready.json``, ``telemetry.ndjson``, and
``result.json`` snapshots.  Telemetry contains request hashes and counts, but
never request bodies, prompt text, credentials, or filesystem paths.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import socketserver
import struct
import sys
import threading
from typing import Any, Final, NoReturn


SCRIPT_SCHEMA_VERSION: Final = "pkv.r4.openai-compatible-script.v1"
STATE_SCHEMA_VERSION: Final = "pkv.r4.openai-compatible-state.v1"
TELEMETRY_SCHEMA_VERSION: Final = "pkv.r4.openai-compatible-telemetry.v1"
RESULT_SCHEMA_VERSION: Final = "pkv.r4.openai-compatible-result.v1"
HARNESS_VERSION: Final = "1.0.0"
LOOPBACK_HOST: Final = "127.0.0.1"
MAX_REQUEST_BYTES: Final = 1_048_576
MAX_SCRIPT_BYTES: Final = 1_048_576
MAX_CONTROL_BYTES: Final = 4_096
MAX_STEPS: Final = 256
MAX_TEXT_CHARS: Final = 65_536
MAX_TOKEN_COUNT: Final = 1_000_000_000
MIN_BARRIER_TIMEOUT_SECONDS: Final = 1
MAX_BARRIER_TIMEOUT_SECONDS: Final = 300
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_BEARER = re.compile(r"Bearer [^\s]{1,512}\Z")


class HarnessContractError(Exception):
    """A safe, code-only contract error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: ARG002
        raise HarnessContractError("invalid_arguments")


def _exact_keys(value: dict[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise HarnessContractError(code)


def _require_dict(value: Any, code: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise HarnessContractError(code)
    return value


def _require_identifier(value: Any, code: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise HarnessContractError(code)
    return value


def _require_text(value: Any, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_TEXT_CHARS
        or "\x00" in value
    ):
        raise HarnessContractError(code)
    return value


def _require_int(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    code: str,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise HarnessContractError(code)
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _reject_json_constant(value: str) -> NoReturn:  # noqa: ARG001
    raise HarnessContractError("invalid_json")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessContractError("duplicate_json_field")
        result[key] = value
    return result


def _decode_json(data: bytes, *, error_code: str) -> Any:
    try:
        text = data.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except HarnessContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise HarnessContractError(error_code) from None


def _validate_usage(value: Any, *, chat: bool) -> dict[str, int]:
    usage = _require_dict(value, "invalid_usage")
    expected = {"prompt_tokens", "completion_tokens"} if chat else {"prompt_tokens"}
    _exact_keys(usage, expected, "invalid_usage_fields")
    prompt_tokens = _require_int(
        usage["prompt_tokens"],
        minimum=0,
        maximum=MAX_TOKEN_COUNT,
        code="invalid_prompt_tokens",
    )
    if not chat:
        return {"prompt_tokens": prompt_tokens}
    completion_tokens = _require_int(
        usage["completion_tokens"],
        minimum=0,
        maximum=MAX_TOKEN_COUNT,
        code="invalid_completion_tokens",
    )
    if prompt_tokens + completion_tokens > MAX_TOKEN_COUNT:
        raise HarnessContractError("invalid_total_tokens")
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def _validate_vector(value: Any, dimension: int) -> list[float]:
    if type(value) is not list or len(value) != dimension:
        raise HarnessContractError("invalid_embedding_vector")
    vector: list[float] = []
    for component in value:
        if type(component) not in (int, float):
            raise HarnessContractError("invalid_embedding_component")
        normalized = float(component)
        if not math.isfinite(normalized) or abs(normalized) > 1_000_000:
            raise HarnessContractError("invalid_embedding_component")
        vector.append(normalized)
    return vector


def _validate_response(
    raw: Any,
    *,
    endpoint: str,
    dimension: int,
) -> dict[str, Any]:
    response = _require_dict(raw, "invalid_response")
    kind = response.get("kind")
    if kind == "error":
        _exact_keys(response, {"kind", "status", "code"}, "invalid_error_fields")
        status = _require_int(
            response["status"],
            minimum=400,
            maximum=599,
            code="invalid_error_status",
        )
        code = _require_identifier(response["code"], "invalid_error_code")
        return {"kind": "error", "status": status, "code": code}

    if kind != "success":
        raise HarnessContractError("invalid_response_kind")
    if endpoint == "/v1/chat/completions":
        _exact_keys(
            response,
            {"kind", "content", "usage"},
            "invalid_chat_response_fields",
        )
        return {
            "kind": "success",
            "content": _require_text(response["content"], "invalid_chat_content"),
            "usage": _validate_usage(response["usage"], chat=True),
        }

    _exact_keys(
        response,
        {"kind", "vectors", "usage"},
        "invalid_embedding_response_fields",
    )
    raw_vectors = response["vectors"]
    if type(raw_vectors) is not list or not raw_vectors or len(raw_vectors) > 1_024:
        raise HarnessContractError("invalid_embedding_vectors")
    return {
        "kind": "success",
        "vectors": [_validate_vector(vector, dimension) for vector in raw_vectors],
        "usage": _validate_usage(response["usage"], chat=False),
    }


def _validate_script(raw: Any) -> dict[str, Any]:
    script = _require_dict(raw, "invalid_script")
    _exact_keys(
        script,
        {
            "schema_version",
            "scenario_id",
            "chat_model",
            "embedding_model",
            "embedding_dimension",
            "barrier_timeout_seconds",
            "steps",
        },
        "invalid_script_fields",
    )
    if script["schema_version"] != SCRIPT_SCHEMA_VERSION:
        raise HarnessContractError("unsupported_script_schema")
    scenario_id = _require_identifier(script["scenario_id"], "invalid_scenario_id")
    chat_model = _require_identifier(script["chat_model"], "invalid_chat_model")
    embedding_model = _require_identifier(
        script["embedding_model"], "invalid_embedding_model"
    )
    dimension = _require_int(
        script["embedding_dimension"],
        minimum=1,
        maximum=65_536,
        code="invalid_embedding_dimension",
    )
    timeout = _require_int(
        script["barrier_timeout_seconds"],
        minimum=MIN_BARRIER_TIMEOUT_SECONDS,
        maximum=MAX_BARRIER_TIMEOUT_SECONDS,
        code="invalid_barrier_timeout",
    )
    raw_steps = script["steps"]
    if type(raw_steps) is not list or not raw_steps or len(raw_steps) > MAX_STEPS:
        raise HarnessContractError("invalid_steps")

    steps: list[dict[str, Any]] = []
    step_ids: set[str] = set()
    allowed_endpoints = {"/v1/chat/completions", "/v1/embeddings"}
    for raw_step in raw_steps:
        step = _require_dict(raw_step, "invalid_step")
        _exact_keys(
            step,
            {"step_id", "endpoint", "barrier", "response"},
            "invalid_step_fields",
        )
        step_id = _require_identifier(step["step_id"], "invalid_step_id")
        if step_id in step_ids:
            raise HarnessContractError("duplicate_step_id")
        step_ids.add(step_id)
        endpoint = step["endpoint"]
        if type(endpoint) is not str or endpoint not in allowed_endpoints:
            raise HarnessContractError("invalid_endpoint")
        if type(step["barrier"]) is not bool:
            raise HarnessContractError("invalid_barrier")
        steps.append(
            {
                "step_id": step_id,
                "endpoint": endpoint,
                "barrier": step["barrier"],
                "response": _validate_response(
                    step["response"],
                    endpoint=endpoint,
                    dimension=dimension,
                ),
            }
        )
    return {
        "schema_version": SCRIPT_SCHEMA_VERSION,
        "scenario_id": scenario_id,
        "chat_model": chat_model,
        "embedding_model": embedding_model,
        "embedding_dimension": dimension,
        "barrier_timeout_seconds": timeout,
        "steps": steps,
    }


def _load_script(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_SCRIPT_BYTES + 1)
    except OSError:
        raise HarnessContractError("script_unavailable") from None
    if not data or len(data) > MAX_SCRIPT_BYTES:
        raise HarnessContractError("invalid_script_size")
    return _validate_script(_decode_json(data, error_code="invalid_script_json"))


class AtomicState:
    """Small durable snapshots without exposing state paths in events."""

    def __init__(self, state_dir: Path):
        self._directory = state_dir
        self._lock = threading.Lock()
        self._temporary_sequence = 0
        self._telemetry: list[dict[str, Any]] = []
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            if not self._directory.is_dir():
                raise OSError
        except OSError:
            raise HarnessContractError("state_directory_unavailable") from None

    def _atomic_write_unlocked(self, filename: str, payload: bytes) -> None:
        self._temporary_sequence += 1
        temporary = self._directory / (
            f".{filename}.{os.getpid()}.{self._temporary_sequence}.tmp"
        )
        target = self._directory / filename
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                directory_descriptor = os.open(self._directory, os.O_RDONLY)
            except OSError:
                directory_descriptor = None
            if directory_descriptor is not None:
                try:
                    os.fsync(directory_descriptor)
                except OSError:
                    pass
                finally:
                    os.close(directory_descriptor)
        except OSError:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise HarnessContractError("state_write_failed") from None

    def write_json(self, filename: str, value: dict[str, Any]) -> None:
        payload = _canonical_json_bytes(value) + b"\n"
        with self._lock:
            self._atomic_write_unlocked(filename, payload)

    def append_telemetry(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._telemetry.append(record)
            payload = b"".join(
                _canonical_json_bytes(item) + b"\n" for item in self._telemetry
            )
            self._atomic_write_unlocked("telemetry.ndjson", payload)


class HarnessRuntime:
    def __init__(self, script: dict[str, Any], state: AtomicState):
        self.script = script
        self.state = state
        self.condition = threading.Condition()
        self.output_lock = threading.Lock()
        self.step_index = 0
        self.request_count = 0
        self.telemetry_sequence = 0
        self.continue_step_id: str | None = None
        self.waiting_step_id: str | None = None
        self.shutdown_requested = False
        self.failed_code: str | None = None
        self.server: HarnessHTTPServer | None = None

    @property
    def scenario_id(self) -> str:
        return self.script["scenario_id"]

    def emit(self, event: str, **fields: Any) -> None:
        record = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "event": event,
            **fields,
        }
        with self.output_lock:
            sys.stdout.buffer.write(_canonical_json_bytes(record) + b"\n")
            sys.stdout.buffer.flush()

    def record(self, event: str, **fields: Any) -> None:
        with self.condition:
            self.telemetry_sequence += 1
            sequence = self.telemetry_sequence
        self.state.append_telemetry(
            {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "sequence": sequence,
                "scenario_id": self.scenario_id,
                "event": event,
                **fields,
            }
        )

    def result(self, outcome: str, *, failure_code: str | None = None) -> None:
        completed = [
            step["step_id"] for step in self.script["steps"][: self.step_index]
        ]
        value: dict[str, Any] = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "harness_version": HARNESS_VERSION,
            "scenario_id": self.scenario_id,
            "outcome": outcome,
            "request_count": self.request_count,
            "completed_step_count": len(completed),
            "completed_step_ids": completed,
            "expected_step_count": len(self.script["steps"]),
        }
        if failure_code is not None:
            value["failure_code"] = failure_code
        self.state.write_json("result.json", value)

    def current_step(self) -> dict[str, Any]:
        with self.condition:
            if self.failed_code is not None:
                raise HarnessContractError("harness_failed")
            if self.shutdown_requested:
                raise HarnessContractError("harness_shutting_down")
            if self.step_index >= len(self.script["steps"]):
                raise HarnessContractError("unexpected_extra_request")
            return self.script["steps"][self.step_index]

    def note_request(self) -> int:
        with self.condition:
            self.request_count += 1
            return self.request_count

    def wait_for_barrier(
        self,
        step_id: str,
        connection: socket.socket,
    ) -> str:
        with self.condition:
            self.waiting_step_id = step_id
            self.condition.notify_all()
        deadline_seconds = self.script["barrier_timeout_seconds"]
        # Event.wait is deliberately avoided: the condition permits immediate
        # controller wake-up, while select observes a killed client connection.
        import time

        deadline = time.monotonic() + deadline_seconds
        try:
            while True:
                with self.condition:
                    if self.shutdown_requested:
                        return "shutdown"
                    if self.continue_step_id == step_id:
                        self.continue_step_id = None
                        return "continue"
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return "timeout"
                try:
                    readable, _, _ = select.select([connection], [], [], 0)
                    if readable:
                        peek = connection.recv(1, socket.MSG_PEEK)
                        if peek == b"":
                            return "abandoned"
                except (OSError, ValueError):
                    return "abandoned"
                with self.condition:
                    self.condition.wait(timeout=min(remaining, 0.1))
        finally:
            with self.condition:
                if self.waiting_step_id == step_id:
                    self.waiting_step_id = None

    def complete_step(self, step_id: str) -> None:
        with self.condition:
            if self.step_index >= len(self.script["steps"]):
                raise HarnessContractError("step_cursor_overflow")
            if self.script["steps"][self.step_index]["step_id"] != step_id:
                raise HarnessContractError("step_cursor_mismatch")
            self.step_index += 1
            completed = self.step_index
            done = completed == len(self.script["steps"])
        self.record(
            "step_completed",
            step_id=step_id,
            completed_step_count=completed,
        )
        self.emit(
            "step_completed",
            step_id=step_id,
            completed_step_count=completed,
        )
        if done:
            self.result("completed")

    def fail(self, code: str) -> None:
        with self.condition:
            if self.failed_code is not None:
                return
            self.failed_code = code
            self.condition.notify_all()
        try:
            self.record("harness_failed", failure_code=code)
            self.result("failed", failure_code=code)
            self.emit("harness_failed", failure_code=code)
        except HarnessContractError:
            pass

    def request_shutdown(self) -> None:
        with self.condition:
            self.shutdown_requested = True
            self.condition.notify_all()
        server = self.server
        if server is not None:
            server.shutdown()

    def fail_and_request_shutdown(self, code: str) -> None:
        self.fail(code)
        # HTTPServer.shutdown must run on a thread other than serve_forever.
        threading.Thread(target=self.request_shutdown, daemon=True).start()

    def accept_control(self, line: bytes) -> None:
        try:
            command = line.decode("ascii", errors="strict").rstrip("\r\n")
        except UnicodeDecodeError:
            self.fail("invalid_control_command")
            self.request_shutdown()
            return
        if command == "SHUTDOWN":
            self.request_shutdown()
            return
        if not command.startswith("CONTINUE "):
            self.fail("invalid_control_command")
            self.request_shutdown()
            return
        step_id = command.removeprefix("CONTINUE ")
        with self.condition:
            if step_id != self.waiting_step_id or self.continue_step_id is not None:
                invalid = True
            else:
                invalid = False
                self.continue_step_id = step_id
                self.condition.notify_all()
        if invalid:
            self.fail("unexpected_continue")
            self.request_shutdown()


class HarnessHTTPServer(http.server.HTTPServer):
    address_family = socket.AF_INET
    allow_reuse_address = False

    def __init__(self, runtime: HarnessRuntime):
        self.runtime = runtime
        super().__init__((LOOPBACK_HOST, 0), HarnessRequestHandler)

    def server_bind(self) -> None:
        # ``HTTPServer.server_bind`` calls socket.getfqdn(), which is a DNS
        # operation even when the bind address is an IPv4 literal.  Bypass it
        # and populate the two HTTPServer metadata fields from the bound socket.
        socketserver.TCPServer.server_bind(self)
        _host, self.server_port = self.server_address[:2]
        self.server_name = LOOPBACK_HOST

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: ARG002
        self.runtime.fail_and_request_shutdown("request_handler_failed")


class HarnessRequestHandler(http.server.BaseHTTPRequestHandler):
    server: HarnessHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "PKVR4Harness/1"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ARG002
        return

    def _error_payload(self, code: str) -> dict[str, Any]:
        return {
            "error": {
                "message": "deterministic provider request rejected",
                "type": "invalid_request_error",
                "param": None,
                "code": code,
            }
        }

    def _send_json(self, status: int, payload: dict[str, Any]) -> bool:
        body = _canonical_json_bytes(payload)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True
            return True
        except OSError:
            self.close_connection = True
            return False

    def _reject(self, code: str, status: int = 400) -> None:
        self._send_json(status, self._error_payload("harness_contract_violation"))
        self.server.runtime.fail_and_request_shutdown(code)

    def _single_header(self, name: str, code: str) -> str:
        values = self.headers.get_all(name, failobj=[])
        if len(values) != 1 or type(values[0]) is not str:
            raise HarnessContractError(code)
        return values[0]

    def _read_request(self) -> tuple[dict[str, Any], bytes]:
        expected_host = f"{LOOPBACK_HOST}:{self.server.server_port}"
        if self._single_header("Host", "invalid_host") != expected_host:
            raise HarnessContractError("invalid_host")
        authorization = self._single_header("Authorization", "invalid_authorization")
        if _BEARER.fullmatch(authorization) is None:
            raise HarnessContractError("invalid_authorization")
        if self._single_header("Content-Type", "invalid_content_type").lower() != (
            "application/json"
        ):
            raise HarnessContractError("invalid_content_type")
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise HarnessContractError("transfer_encoding_forbidden")
        content_lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(content_lengths) != 1 or not content_lengths[0].isdigit():
            raise HarnessContractError("invalid_content_length")
        content_length = int(content_lengths[0])
        if not 0 < content_length <= MAX_REQUEST_BYTES:
            raise HarnessContractError("invalid_content_length")
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            raise HarnessContractError("truncated_request")
        payload = _decode_json(body, error_code="invalid_request_json")
        return _require_dict(payload, "invalid_request_object"), body

    def _validate_chat_request(self, payload: dict[str, Any]) -> tuple[int, str]:
        required = {"model", "messages", "temperature", "max_tokens"}
        allowed = required | {"stream"}
        if not required <= set(payload) or not set(payload) <= allowed:
            raise HarnessContractError("invalid_chat_request_fields")
        runtime = self.server.runtime
        if payload["model"] != runtime.script["chat_model"]:
            raise HarnessContractError("chat_model_mismatch")
        if "stream" in payload and payload["stream"] is not False:
            raise HarnessContractError("streaming_forbidden")
        temperature = payload["temperature"]
        if (
            type(temperature) not in (int, float)
            or not math.isfinite(float(temperature))
            or not 0 <= float(temperature) <= 2
        ):
            raise HarnessContractError("invalid_temperature")
        _require_int(
            payload["max_tokens"],
            minimum=1,
            maximum=1_000_000,
            code="invalid_max_tokens",
        )
        messages = payload["messages"]
        if type(messages) is not list or not messages or len(messages) > 128:
            raise HarnessContractError("invalid_messages")
        content_values: list[str] = []
        for message in messages:
            item = _require_dict(message, "invalid_message")
            _exact_keys(item, {"role", "content"}, "invalid_message_fields")
            if item["role"] not in {"system", "user", "assistant"}:
                raise HarnessContractError("invalid_message_role")
            content_values.append(
                _require_text(item["content"], "invalid_message_content")
            )
        total_chars = sum(len(content) for content in content_values)
        if total_chars > MAX_TEXT_CHARS:
            raise HarnessContractError("messages_too_large")
        return len(messages), _sha256(content_values)

    def _validate_embedding_request(
        self, payload: dict[str, Any]
    ) -> tuple[int, str, str]:
        required = {"model", "input"}
        allowed = required | {"dimensions", "encoding_format"}
        if not required <= set(payload) or not set(payload) <= allowed:
            raise HarnessContractError("invalid_embedding_request_fields")
        runtime = self.server.runtime
        if payload["model"] != runtime.script["embedding_model"]:
            raise HarnessContractError("embedding_model_mismatch")
        if (
            "dimensions" in payload
            and payload["dimensions"] != runtime.script["embedding_dimension"]
        ):
            raise HarnessContractError("embedding_dimension_mismatch")
        encoding_format = payload.get("encoding_format", "float")
        if encoding_format not in {"float", "base64"}:
            raise HarnessContractError("invalid_encoding_format")
        raw_input = payload["input"]
        if type(raw_input) is str:
            values = [_require_text(raw_input, "invalid_embedding_input")]
        elif type(raw_input) is list and raw_input and len(raw_input) <= 1_024:
            values = [
                _require_text(value, "invalid_embedding_input") for value in raw_input
            ]
        else:
            raise HarnessContractError("invalid_embedding_input")
        if sum(len(value) for value in values) > MAX_TEXT_CHARS:
            raise HarnessContractError("embedding_input_too_large")
        return len(values), _sha256(values), encoding_format

    def _success_payload(
        self,
        step: dict[str, Any],
        item_count: int,
        encoding_format: str,
    ) -> dict[str, Any]:
        runtime = self.server.runtime
        response = step["response"]
        if step["endpoint"] == "/v1/chat/completions":
            usage = response["usage"]
            return {
                "id": f"chatcmpl-{step['step_id']}",
                "object": "chat.completion",
                "created": 0,
                "model": runtime.script["chat_model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response["content"],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": usage["prompt_tokens"],
                    "completion_tokens": usage["completion_tokens"],
                    "total_tokens": usage["prompt_tokens"] + usage["completion_tokens"],
                },
            }
        vectors = response["vectors"]
        if len(vectors) != item_count:
            raise HarnessContractError("embedding_response_count_mismatch")
        usage = response["usage"]
        response_vectors: list[list[float] | str]
        if encoding_format == "base64":
            response_vectors = [
                base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode(
                    "ascii"
                )
                for vector in vectors
            ]
        else:
            response_vectors = vectors
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(response_vectors)
            ],
            "model": runtime.script["embedding_model"],
            "usage": {
                "prompt_tokens": usage["prompt_tokens"],
                "total_tokens": usage["prompt_tokens"],
            },
        }

    def do_POST(self) -> None:  # noqa: N802
        runtime = self.server.runtime
        try:
            if "?" in self.path or "#" in self.path:
                raise HarnessContractError("request_target_forbidden")
            step = runtime.current_step()
            if self.path != step["endpoint"]:
                raise HarnessContractError("unexpected_endpoint")
            payload, _body = self._read_request()
            encoding_format = "float"
            if step["endpoint"] == "/v1/chat/completions":
                item_count, content_sha256 = self._validate_chat_request(payload)
            else:
                (
                    item_count,
                    content_sha256,
                    encoding_format,
                ) = self._validate_embedding_request(payload)
            request_sha256 = _sha256(payload)
            request_number = runtime.note_request()
            event_fields = {
                "step_id": step["step_id"],
                "endpoint": step["endpoint"],
                "request_number": request_number,
                "request_sha256": request_sha256,
                "content_sha256": content_sha256,
                "item_count": item_count,
                "body_byte_count": len(_body),
                "barrier": step["barrier"],
            }
            runtime.record("request_waiting", **event_fields)
            runtime.emit("request_waiting", **event_fields)

            if step["barrier"]:
                outcome = runtime.wait_for_barrier(step["step_id"], self.connection)
                if outcome == "abandoned":
                    runtime.record(
                        "request_abandoned",
                        step_id=step["step_id"],
                        request_number=request_number,
                    )
                    self.close_connection = True
                    return
                if outcome == "shutdown":
                    self._send_json(503, self._error_payload("harness_shutting_down"))
                    return
                if outcome == "timeout":
                    self._send_json(504, self._error_payload("barrier_timeout"))
                    runtime.fail_and_request_shutdown("barrier_timeout")
                    return

            response = step["response"]
            if response["kind"] == "error":
                sent = self._send_json(
                    response["status"],
                    {
                        "error": {
                            "message": "deterministic provider failure",
                            "type": "server_error",
                            "param": None,
                            "code": response["code"],
                        }
                    },
                )
            else:
                sent = self._send_json(
                    200,
                    self._success_payload(step, item_count, encoding_format),
                )
            if sent:
                runtime.complete_step(step["step_id"])
            else:
                runtime.record(
                    "response_abandoned",
                    step_id=step["step_id"],
                    request_number=request_number,
                )
        except HarnessContractError as exc:
            self._reject(exc.code)
        except Exception:
            self._reject("request_processing_failed", status=500)

    def do_GET(self) -> None:  # noqa: N802
        self._reject("method_forbidden", status=405)

    def do_PUT(self) -> None:  # noqa: N802
        self._reject("method_forbidden", status=405)

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject("method_forbidden", status=405)

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject("method_forbidden", status=405)


def _control_loop(runtime: HarnessRuntime) -> None:
    while True:
        try:
            line = sys.stdin.buffer.readline(MAX_CONTROL_BYTES + 1)
        except OSError:
            line = b""
        if not line:
            runtime.request_shutdown()
            return
        if len(line) > MAX_CONTROL_BYTES or not line.endswith((b"\n", b"\r")):
            runtime.fail("invalid_control_command")
            runtime.request_shutdown()
            return
        runtime.accept_control(line)
        if runtime.shutdown_requested:
            return


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = _SafeArgumentParser(add_help=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--state-dir", required=True)
    return parser.parse_args(argv)


def _run(argv: list[str]) -> int:
    arguments = _parse_arguments(argv)
    script = _load_script(Path(arguments.script))
    state = AtomicState(Path(arguments.state_dir))
    runtime = HarnessRuntime(script, state)
    server = HarnessHTTPServer(runtime)
    runtime.server = server
    port = server.server_port
    base_url = f"http://{LOOPBACK_HOST}:{port}/v1"
    ready = {
        "schema_version": STATE_SCHEMA_VERSION,
        "harness_version": HARNESS_VERSION,
        "scenario_id": runtime.scenario_id,
        "bind_host": LOOPBACK_HOST,
        "port": port,
        "base_url": base_url,
        "chat_model": script["chat_model"],
        "embedding_model": script["embedding_model"],
        "embedding_dimension": script["embedding_dimension"],
        "expected_step_count": len(script["steps"]),
    }
    state.write_json("ready.json", ready)
    runtime.result("running")
    runtime.record("ready", port=port, expected_step_count=len(script["steps"]))

    def request_signal_shutdown(signum: int, frame: Any) -> None:  # noqa: ARG001
        threading.Thread(target=runtime.request_shutdown, daemon=True).start()

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is not None:
            signal.signal(signal_number, request_signal_shutdown)

    control_thread = threading.Thread(
        target=_control_loop,
        args=(runtime,),
        name="pkv-r4-harness-control",
        daemon=True,
    )
    control_thread.start()
    runtime.emit(
        "ready",
        port=port,
        base_url=base_url,
        expected_step_count=len(script["steps"]),
    )
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
        if runtime.failed_code is not None:
            runtime.result("failed", failure_code=runtime.failed_code)
        elif runtime.step_index == len(script["steps"]):
            runtime.result("completed")
        else:
            runtime.result("shutdown")
    return 1 if runtime.failed_code is not None else 0


def main() -> int:
    try:
        return _run(sys.argv[1:])
    except HarnessContractError as exc:
        safe = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "event": "startup_failed",
            "failure_code": exc.code,
        }
        sys.stderr.buffer.write(_canonical_json_bytes(safe) + b"\n")
        sys.stderr.buffer.flush()
        return 2
    except Exception:
        safe = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "event": "startup_failed",
            "failure_code": "unexpected_startup_failure",
        }
        sys.stderr.buffer.write(_canonical_json_bytes(safe) + b"\n")
        sys.stderr.buffer.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
