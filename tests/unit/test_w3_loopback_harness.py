"""Packaging-contract tests for the external W3 deterministic loopback harness."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest


pytestmark = pytest.mark.packaging_contract

ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / "packaging" / "harness"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = _load_module(
    "loopback_provider",
    HARNESS_ROOT / "loopback_provider.py",
)
manifest_builder = _load_module(
    "pkv_w3_loopback_manifest_builder",
    HARNESS_ROOT / "build_manifest.py",
)


def test_manifest_builder_loads_exact_sibling_with_python_safe_path(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["PYTHONSAFEPATH"] = "1"
    completed = subprocess.run(
        [sys.executable, str(HARNESS_ROOT / "build_manifest.py"), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--toolchain-lock-sha256" in completed.stdout


def _script(name: str):
    return harness.load_script(HARNESS_ROOT / "scripts" / name)


def _request(
    user_message: str,
    *,
    roles_and_contents: list[tuple[str, str]] | None = None,
) -> dict:
    messages = roles_and_contents or [("user", user_message)]
    return {
        "model": "pkv-loopback-chat-v1",
        "messages": [{"role": role, "content": content} for role, content in messages],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": 128,
        "temperature": 0.2,
    }


def _bundle(script_name: str):
    script_path = HARNESS_ROOT / "scripts" / script_name
    contract_path = HARNESS_ROOT / "contract.v1.json"
    contract = harness.validate_contract(
        harness.load_json_file(contract_path, label="contract")
    )
    return harness.ManifestBundle(
        manifest_path=HARNESS_ROOT / "manifest.source.v1.json",
        manifest_sha256="1" * 64,
        manifest={},
        runtime_path=HARNESS_ROOT / "loopback_provider.py",
        contract_path=contract_path,
        contract=contract,
        contract_sha256=harness.sha256_file(contract_path),
        script_path=script_path,
        script=harness.load_script(script_path),
    )


def _start_server(tmp_path: Path, script_name: str):
    telemetry_path = tmp_path / "telemetry.ndjson"
    telemetry_handle = telemetry_path.open("xb")
    bundle = _bundle(script_name)
    state = harness.HarnessState(bundle.script, telemetry_handle)
    server = harness.LoopbackServer(
        (harness.LOOPBACK_HOST, 0),
        harness.LoopbackRequestHandler,
        bundle=bundle,
        state=state,
        harness_sha256="2" * 64,
        frozen=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, state, telemetry_handle


def _stop_server(server, thread, telemetry_handle) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    telemetry_handle.close()


def _raw_exchange(
    server,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    authorization: bool = False,
    stop_after: bytes | None = None,
) -> bytes:
    headers = [
        f"{method} {path} HTTP/1.1",
        f"Host: 127.0.0.1:{server.server_port}",
        "Connection: close",
    ]
    if body:
        headers.extend(
            [
                "Content-Type: application/json",
                f"Content-Length: {len(body)}",
            ]
        )
    if authorization:
        headers.append("Authorization: Bearer synthetic-loopback-key")
    wire = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(3)
    client.connect(("127.0.0.1", server.server_port))
    client.sendall(wire)
    response = bytearray()
    try:
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if stop_after is not None and stop_after in response:
                break
    finally:
        client.close()
    return bytes(response)


def _wait_for_status(state, expected: str, *, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = state.snapshot()
        if snapshot["status"] == expected:
            return snapshot
        time.sleep(0.01)
    pytest.fail(f"harness state did not reach {expected}: {state.snapshot()}")


def test_runtime_is_stdlib_only_and_spec_physically_excludes_product_modules() -> None:
    source_path = HARNESS_ROOT / "loopback_provider.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots <= set(sys.stdlib_module_names) | {"__future__"}

    spec_text = (HARNESS_ROOT / "pkv-loopback-provider.spec").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        '"PySide6"',
        '"httpx"',
        '"openai"',
        '"pytest"',
        '"src"',
    ):
        assert forbidden in spec_text
    assert "upx=False" in spec_text
    assert "console=True" in spec_text


def test_contract_and_all_versioned_scripts_are_strict_and_scenario_complete(
    capsys,
) -> None:
    contract = harness.validate_contract(
        harness.load_json_file(HARNESS_ROOT / "contract.v1.json", label="contract")
    )
    assert contract["network_policy"]["bind_host"] == "127.0.0.1"
    assert contract["request_contract"]["scenario_selection"] == (
        "ordered_external_script_only"
    )

    scripts = {
        path.name: harness.load_script(path)
        for path in sorted((HARNESS_ROOT / "scripts").glob("*.json"))
    }
    assert set(scripts) == {
        "provider-error.v1.json",
        "stop.v1.json",
        "success.v1.json",
        "w4-chat-lifecycle.v1.json",
    }
    lifecycle = scripts["w4-chat-lifecycle.v1.json"]
    assert [step.scenario for step in lifecycle.steps] == [
        "success",
        "slow_stop",
        "provider_error",
    ]
    assert len({step.step_id for step in lifecycle.steps}) == 3

    source_bundle = harness.load_manifest_bundle(
        HARNESS_ROOT / "manifest.source.v1.json",
        HARNESS_ROOT / "scripts" / "success.v1.json",
        actual_runtime_path=HARNESS_ROOT / "loopback_provider.py",
        actual_runtime_kind="source",
    )
    assert source_bundle.manifest["distribution"] == "e2e-only"
    assert source_bundle.manifest["release_payload_membership"] == "forbidden"
    assert (
        harness.main(
            [
                "verify",
                "--manifest",
                str(HARNESS_ROOT / "manifest.source.v1.json"),
                "--script",
                str(HARNESS_ROOT / "scripts" / "success.v1.json"),
            ]
        )
        == harness.EXIT_OK
    )
    verified = json.loads(capsys.readouterr().out)
    assert verified["result"] == "verified"
    assert verified["runtime_kind"] == "source"


def test_duplicate_json_keys_and_request_shape_fail_closed() -> None:
    with pytest.raises(harness.DuplicateKeyError):
        harness.parse_json_bytes(b'{"a":1,"a":2}', label="fixture")

    step = _script("success.v1.json").steps[0]
    valid = _request("pkv-w4-success")
    validated = harness.validate_chat_request(valid, expected=step.expect)
    assert validated.message_roles == ("user",)
    assert validated.last_user_sha256 == step.expect.last_user_sha256

    mutations = []
    for field in ("stream", "stream_options", "model", "max_tokens", "temperature"):
        changed = dict(valid)
        changed[field] = {
            "stream": False,
            "stream_options": {"include_usage": False},
            "model": "scenario-success",
            "max_tokens": True,
            "temperature": float("nan"),
        }[field]
        mutations.append(changed)
    changed = dict(valid)
    changed["scenario"] = "success"
    mutations.append(changed)

    for mutation in mutations:
        with pytest.raises(harness.HarnessContractError):
            harness.validate_chat_request(mutation, expected=step.expect)


def test_manifest_builder_binds_runtime_contract_scripts_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    package = tmp_path / "harness-package"
    scripts_dir = package / "scripts"
    scripts_dir.mkdir(parents=True)
    runtime = package / "loopback_provider.py"
    contract = package / "contract.v1.json"
    shutil.copy2(HARNESS_ROOT / "loopback_provider.py", runtime)
    shutil.copy2(HARNESS_ROOT / "contract.v1.json", contract)
    script_paths = []
    for source in sorted((HARNESS_ROOT / "scripts").glob("*.json")):
        target = scripts_dir / source.name
        shutil.copy2(source, target)
        script_paths.append(target)
    output = package / "harness-manifest.json"
    manifest = manifest_builder.build_manifest(
        output=output,
        runtime=runtime,
        runtime_kind="source",
        contract=contract,
        scripts=script_paths,
        source_revision="fixture-revision",
        build_fingerprint_sha256="3" * 64,
        toolchain_lock_sha256="4" * 64,
    )
    manifest_builder.atomic_publish(
        output,
        harness.canonical_json_bytes(manifest) + b"\n",
    )

    bundle = harness.load_manifest_bundle(
        output,
        scripts_dir / "success.v1.json",
        actual_runtime_path=runtime,
        actual_runtime_kind="source",
    )
    assert bundle.manifest["distribution"] == "e2e-only"
    assert bundle.manifest["release_payload_membership"] == "forbidden"
    assert bundle.script.script_id == "w3.chat.success.v1"

    success_path = scripts_dir / "success.v1.json"
    success_path.write_bytes(success_path.read_bytes() + b"\n")
    with pytest.raises(harness.HarnessContractError, match="hash"):
        harness.load_manifest_bundle(output, success_path)


def test_success_wire_is_strict_sse_finish_usage_done_order(tmp_path: Path) -> None:
    server, thread, state, telemetry = _start_server(tmp_path, "success.v1.json")
    try:
        health_response = _raw_exchange(
            server,
            method="GET",
            path="/health",
        )
        health = json.loads(health_response.split(b"\r\n\r\n", 1)[1])
        assert health["schema_version"] == harness.HEALTH_SCHEMA
        assert health["host"] == "127.0.0.1"
        assert health["status"] == "ready"
        contract_response = _raw_exchange(
            server,
            method="GET",
            path="/contract",
        )
        contract_payload = json.loads(contract_response.split(b"\r\n\r\n", 1)[1])
        assert contract_payload["schema_version"] == (harness.CONTRACT_RESPONSE_SCHEMA)
        assert contract_payload["contract"]["contract_id"] == harness.CONTRACT_ID

        body = harness.canonical_json_bytes(_request("pkv-w4-success"))
        response = _raw_exchange(
            server,
            method="POST",
            path="/v1/chat/completions",
            body=body,
            authorization=True,
        )
        assert response.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"Date:" not in response
        markers = [
            b'"role":"assistant"',
            b"PKV_W4_SUCCESS_",
            b'"finish_reason":"stop"',
            b'"choices":[],"created":0',
            b"data: [DONE]",
        ]
        positions = [response.index(marker) for marker in markers]
        assert positions == sorted(positions)
        snapshot = _wait_for_status(state, "complete")
        assert [record["event"] for record in snapshot["records"]] == [
            "request_accepted",
            "stream_completed",
        ]
        terminal = snapshot["records"][-1]
        assert terminal["finish_sent"] is True
        assert terminal["usage_sent"] is True
        assert terminal["done_sent"] is True
        assert "pkv-w4-success" not in str(snapshot)
        assert "synthetic-loopback-key" not in str(snapshot)
    finally:
        _stop_server(server, thread, telemetry)


def test_slow_stop_requires_observed_client_disconnect_and_never_finishes(
    tmp_path: Path,
) -> None:
    server, thread, state, telemetry = _start_server(tmp_path, "stop.v1.json")
    try:
        body = harness.canonical_json_bytes(_request("pkv-w4-stop"))
        response = _raw_exchange(
            server,
            method="POST",
            path="/v1/chat/completions",
            body=body,
            authorization=True,
            stop_after=b"PKV_W4_STOP_PARTIAL_V1",
        )
        assert b"PKV_W4_STOP_PARTIAL_V1" in response
        assert b'"finish_reason":"stop"' not in response
        assert b"data: [DONE]" not in response
        snapshot = _wait_for_status(state, "complete")
        terminal = snapshot["records"][-1]
        assert terminal["event"] == "client_cancelled"
        assert terminal["client_disconnected"] is True
        assert terminal["finish_sent"] is False
        assert terminal["usage_sent"] is False
        assert terminal["done_sent"] is False
    finally:
        _stop_server(server, thread, telemetry)


def test_provider_error_is_one_sanitized_503_and_script_exhaustion_fails_closed(
    tmp_path: Path,
) -> None:
    server, thread, state, telemetry = _start_server(
        tmp_path,
        "provider-error.v1.json",
    )
    try:
        body = harness.canonical_json_bytes(_request("pkv-w4-error"))
        response = _raw_exchange(
            server,
            method="POST",
            path="/v1/chat/completions",
            body=body,
            authorization=True,
        )
        assert response.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
        assert b'"code":"pkv_fixture_provider_error"' in response
        snapshot = _wait_for_status(state, "complete")
        assert snapshot["records"][-1]["event"] == "provider_error"

        second = _raw_exchange(
            server,
            method="POST",
            path="/v1/chat/completions",
            body=body,
            authorization=True,
        )
        assert second.startswith(b"HTTP/1.1 409 Conflict\r\n")
        assert b'"code":"script_exhausted"' in second
        failed = _wait_for_status(state, "failed")
        assert failed["completed_steps"] == 1
        assert failed["violations"] == ["script_exhausted"]
    finally:
        _stop_server(server, thread, telemetry)


def test_run_server_publishes_ready_telemetry_and_hash_bound_result(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    bundle = _bundle("success.v1.json")
    outcome: list[int] = []

    thread = threading.Thread(
        target=lambda: outcome.append(
            harness.run_server(
                bundle=bundle,
                state_dir=state_dir,
                port=0,
                idle_timeout_seconds=5,
            )
        ),
        daemon=True,
    )
    thread.start()
    ready_path = state_dir / "ready.json"
    deadline = time.monotonic() + 3
    while not ready_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready_path.exists()
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    assert ready["host"] == "127.0.0.1"
    assert ready["frozen"] is False
    assert ready["base_url"] == f"http://127.0.0.1:{ready['port']}/v1"

    server_ref = type("ServerRef", (), {"server_port": ready["port"]})()
    body = harness.canonical_json_bytes(_request("pkv-w4-success"))
    response = _raw_exchange(
        server_ref,
        method="POST",
        path="/v1/chat/completions",
        body=body,
        authorization=True,
    )
    assert response.startswith(b"HTTP/1.1 200 OK\r\n")
    (state_dir / "shutdown.request").write_bytes(harness.SHUTDOWN_CONTENT)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert outcome == [harness.EXIT_OK]

    result = json.loads((state_dir / "result.json").read_text(encoding="utf-8"))
    assert result["result"] == "passed"
    assert result["completed_steps"] == result["total_steps"] == 1
    assert result["telemetry_sha256"] == harness.sha256_file(
        state_dir / "telemetry.ndjson"
    )
    assert result["script_sha256"] == ready["script_sha256"]
    assert result["contract_sha256"] == ready["contract_sha256"]


def test_invalid_request_never_consumes_script_and_marks_result_failed(
    tmp_path: Path,
) -> None:
    server, thread, state, telemetry = _start_server(tmp_path, "success.v1.json")
    try:
        invalid = _request("pkv-w4-success")
        invalid["scenario"] = "success"
        response = _raw_exchange(
            server,
            method="POST",
            path="/v1/chat/completions",
            body=harness.canonical_json_bytes(invalid),
            authorization=True,
        )
        assert response.startswith(b"HTTP/1.1 400 Bad Request\r\n")
        snapshot = _wait_for_status(state, "failed")
        assert snapshot["completed_steps"] == 0
        assert snapshot["active_step_id"] is None
        assert snapshot["violations"] == ["request_contract_invalid"]
    finally:
        _stop_server(server, thread, telemetry)
