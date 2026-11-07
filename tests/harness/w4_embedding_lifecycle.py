"""Payload-external clock and process controls for W4 source acceptance.

The only product substitution is the budget clock. Provider requests use the
normal OpenAI-compatible client and the existing external loopback harness;
publication contention uses a real root writer lease and its response barrier.
No product phase/result/Provider implementation is replaced here.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Iterator, MutableMapping


CLOCK_ENV = "PKV_TEST_EMBEDDING_CLOCK"
CLOCK_SCHEMA = "pkv.test.embedding-clock.v1"
CLOCK_FILENAME = "w4-clock.json"
INITIAL_TIME = "2026-09-30T12:00:00Z"
NEXT_DAY = "2026-10-01T12:00:00Z"
TITLE = "W4 public continuation source"
CONTENT = "A sealed semantic generation survives restart and retains its original token reservation."


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("W4 clock contains duplicate fields")
        result[key] = value
    return result


def consume_clock_request(
    environment: MutableMapping[str, str],
    *,
    target: str,
    load_local: bool,
    synthetic_provider_config: bool,
) -> datetime | None:
    """Consume a strict isolated clock request before importing product code.

    A request is legal only after the normal synthetic Provider configuration
    validator has succeeded. It cannot select another file or enable network.
    The environment variable is removed before any product entrypoint runs.
    """

    raw = environment.pop(CLOCK_ENV, None)
    if raw is None:
        return None
    if load_local or target not in {"cli", "mcp"} or not synthetic_provider_config:
        raise RuntimeError("W4 clock requires an offline synthetic CLI/MCP child")
    if not raw or "\x00" in raw:
        raise RuntimeError("W4 clock path is invalid")
    data_root = Path(environment["DATA_DIR"])
    if not data_root.is_absolute():
        raise RuntimeError("W4 clock DataRoot must be absolute")
    expected = Path(os.path.abspath(data_root / "tmp" / CLOCK_FILENAME))
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RuntimeError("W4 clock path must be absolute")
    candidate = Path(os.path.abspath(candidate))
    if os.path.normcase(str(candidate)) != os.path.normcase(str(expected)):
        raise RuntimeError("W4 clock must use the selected DataRoot clock file")

    # Include all ancestors, not just the leaf or the descendants of DataRoot.
    # Windows junctions are reparse points even when is_symlink() is false.
    leaf_stat = None
    for item in (*reversed(candidate.parents), candidate):
        try:
            observed = os.lstat(item)
        except OSError as exc:
            raise RuntimeError("W4 clock path is unavailable") from exc
        if stat.S_ISLNK(observed.st_mode) or getattr(observed, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("W4 clock path contains a link")
        if item == candidate:
            leaf_stat = observed
        elif not stat.S_ISDIR(observed.st_mode):
            raise RuntimeError("W4 clock ancestor is not a directory")
    if (
        leaf_stat is None
        or not stat.S_ISREG(leaf_stat.st_mode)
        or leaf_stat.st_nlink != 1
        or not 1 <= leaf_stat.st_size <= 4096
    ):
        raise RuntimeError("W4 clock must be a bounded regular file with one link")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                leaf_stat.st_dev, leaf_stat.st_ino, leaf_stat.st_size, leaf_stat.st_mtime_ns
            ) or opened.st_nlink != 1:
                raise RuntimeError("W4 clock file changed during validation")
            raw_bytes = stream.read(4097)
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                raise RuntimeError("W4 clock file changed during validation")
    except OSError as exc:
        raise RuntimeError("W4 clock cannot be opened safely") from exc
    try:
        payload = json.loads(raw_bytes.decode("utf-8", errors="strict"), object_pairs_hook=_strict_object)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("W4 clock must be strict UTF-8 JSON") from exc
    if (
        type(payload) is not dict
        or set(payload) != {"schema", "utc_now"}
        or payload["schema"] != CLOCK_SCHEMA
        or type(payload["utc_now"]) is not str
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["utc_now"]) is None
    ):
        raise RuntimeError("W4 clock fields are invalid")
    try:
        return datetime.strptime(payload["utc_now"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise RuntimeError("W4 clock UTC instant is invalid") from exc


def install_budget_clock(instant: datetime) -> None:
    """Install one fixed budget instant after the offline guards are active."""

    from src.runtime import embedding_rebuild_ledger

    embedding_rebuild_ledger.current_utc = lambda: instant


def write_clock(runtime: Any, instant: str) -> None:
    """Advance only future child processes; an existing host keeps its instant."""

    path = runtime.layout.tmp_dir / CLOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": CLOCK_SCHEMA, "utc_now": instant}), encoding="utf-8")


def create_runtime(root: Path, *, provider_base_url: str, instant: str = INITIAL_TIME) -> Any:
    from tests.blackbox.r4_fullflow_support import R4BlackboxRuntime
    from src.runtime.write_lease import write_lease_scope
    from src.storage.markdown_store import Entry, MarkdownStore
    from src.storage.sqlite_store import SQLiteStore

    class ClockedRuntime(R4BlackboxRuntime):
        def child_env(self) -> dict[str, str]:
            environment = super().child_env()
            environment[CLOCK_ENV] = str(self.layout.tmp_dir / CLOCK_FILENAME)
            return environment

    runtime = ClockedRuntime.create(root, provider_base_url=provider_base_url)
    # Seed only the test-owned input, never rebuild results/operations/proofs.
    with write_lease_scope(runtime.layout):
        markdown = MarkdownStore(runtime.layout.vault_dir)
        entry = Entry(title=TITLE, source_type="text", content=CONTENT)
        plan = markdown.plan_save(entry)
        markdown.save_planned(plan, entry)
        SQLiteStore(runtime.layout.db_path).insert_entry_with_chunks(entry, plan.relative_path, [CONTENT])
    write_clock(runtime, instant)
    return runtime


def provider_script(scenario_id: str, *, rebuilds: int = 1) -> dict[str, Any]:
    from tests.blackbox.r4_fullflow_support import manual_embedding_rebuild_provider_script

    script = manual_embedding_rebuild_provider_script(scenario_id)
    steps = script["steps"][4:6]
    script["steps"] = [
        {**step, "step_id": f"rebuild-{number}-{kind}"}
        for number in range(1, rebuilds + 1)
        for kind, step in zip(("document", "chunks"), steps)
    ]
    return script


def shared_bytes(runtime: Any) -> dict[str, bytes]:
    """Shared durable bytes that D must never mutate without a writer lease."""

    paths = (
        runtime.layout.runtime_state_dir / "embedding-rebuild-ledger.json",
        runtime.layout.runtime_config_path,
        runtime.layout.log_dir / "audit.jsonl",
    )
    return {str(path): path.read_bytes() for path in paths if path.exists()}


@contextmanager
def competing_writer(runtime: Any) -> Iterator[dict[str, bytes]]:
    """Acquire in another thread so a context-local lease cannot leak to tests."""

    from src.runtime.write_lease import write_lease_scope

    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            with write_lease_scope(runtime.layout):
                acquired.set()
                if not release.wait(timeout=90):
                    raise AssertionError("W4 contender was not released")
        except BaseException as exc:
            errors.append(exc)
            acquired.set()

    thread = threading.Thread(target=hold, name="w4-publication-contender", daemon=True)
    thread.start()
    try:
        assert acquired.wait(timeout=10), "W4 contender did not acquire the root lease"
        assert not errors
        before = shared_bytes(runtime)
        yield before
        assert shared_bytes(runtime) == before, "D modified shared durable state without its lease"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors


def envelope(payload: dict[str, Any], operation: str, *, success: bool = True) -> Any:
    assert set(payload) == {"schema", "status", "terminal", "operation", "result", "issues"}
    assert payload["schema"] == "pkv.lifecycle-response.v2"
    assert payload["operation"] == operation
    assert payload["status"] == payload["terminal"] == ("success" if success else "error")
    if not success:
        assert payload["result"] is None
    return payload["result"]


def issue_code(payload: dict[str, Any]) -> str:
    assert payload["issues"]
    return payload["issues"][0]["code"]


def pending_operation(payload: dict[str, Any]) -> str:
    assert issue_code(payload) == "embedding_publication_pending"
    issue = payload["issues"][0]
    assert issue["stage"] == "embedding_publication"
    assert issue["recoverable"] is True
    details = issue["details"]
    operation_id = details["operation_id"]
    assert details == {
        "operation_id": operation_id,
        "effective_state": "awaiting_publish",
        "provider_effect": "completed",
        "publish_only_required": True,
    }
    return operation_id


def ledger_operations(runtime: Any) -> dict[str, Any]:
    payload = json.loads((runtime.layout.runtime_state_dir / "embedding-rebuild-ledger.json").read_text("utf-8"))
    assert payload["schema_version"] == 3
    return payload["operations"]


def assert_sealed_proof(runtime: Any, operation_id: str, inspection: dict[str, Any]) -> dict[str, bytes]:
    """Independently observe the persisted seal, rather than trust its DTO."""

    record = ledger_operations(runtime)[operation_id]
    stage = runtime.layout.vector_index_dir / "staging" / record["stage_key"]
    raw = (stage / ".publish-ready.json").read_bytes()
    proof = json.loads(raw)
    assert proof["schema"] == "pkv.embedding.publish-ready.v1"
    assert hashlib.sha256(raw).hexdigest() == inspection["proof_sha256"]
    assert raw == (json.dumps(proof, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    assert proof["operation_id"] == operation_id
    assert proof["public_plan_id"] == inspection["original_plan_id"]
    assert proof["provider_effect"] == "completed"
    assert proof["provider_call_count"] == 2
    assert proof["provider_usage"]["embedding_input_tokens"] == 34
    grant_bytes = (stage / ".generation-stage-grant.json").read_bytes()
    grant = json.loads(grant_bytes)
    assert grant["schema_version"] == 2
    assert hashlib.sha256(grant_bytes).hexdigest() == proof["grant_sha256"]
    assert grant["issued_at_ms"] <= proof["sealed_at_ms"] <= grant["expires_at_ms"]
    artifacts = {name: (stage / name).read_bytes() for name in proof["artifact_sha256"]}
    assert set(artifacts) == {"doc_vectors.idx", "doc_vectors_metadata.json", "chunk_vectors.idx", "chunk_vectors_metadata.json"}
    for name, content in artifacts.items():
        assert hashlib.sha256(content).hexdigest() == proof["artifact_sha256"][name]
    manifest = (stage / "generation-manifest.json").read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == proof["manifest_sha256"]
    return {**artifacts, "generation-manifest.json": manifest, ".publish-ready.json": raw, ".generation-stage-grant.json": grant_bytes}


def assert_publication_durable(runtime: Any, operation_id: str, saved_stage: dict[str, bytes]) -> None:
    """Final generation contains only public artifacts; proof/audit survive."""

    record = ledger_operations(runtime)[operation_id]
    stage = runtime.layout.vector_index_dir / "staging" / record["stage_key"]
    assert {name: (stage / name).read_bytes() for name in saved_stage} == saved_stage
    final = runtime.layout.vector_index_dir / "generations" / record["generation_id"]
    expected = {name: content for name, content in saved_stage.items() if not name.startswith(".")}
    assert {path.name: path.read_bytes() for path in final.iterdir()} == expected
    events = [json.loads(line)["event"] for line in (runtime.layout.log_dir / "audit.jsonl").read_text("utf-8").splitlines()]
    completed = [event for event in events if event.get("operation") == "embedding_rebuild" and event.get("phase") == "completed" and event.get("result", {}).get("operation_id") == operation_id]
    assert len(completed) == 1
    result = completed[0]["result"]
    material = {"operation_id": operation_id, "generation_id": record["generation_id"], "manifest_sha256": hashlib.sha256(expected["generation-manifest.json"]).hexdigest()}
    expected_id = hashlib.sha256(b"pkv.embedding.audit-completion.v1\0" + json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert all(result[name] == value for name, value in material.items())
    assert result["completion_id"] == expected_id


def configure_budget(runtime: Any, *, daily: int, monthly: int) -> None:
    """Change the public synthetic user configuration before planning work."""

    payload = json.loads(runtime.user_config_path.read_text("utf-8"))
    payload["ai"]["embedding_rebuild"]["token_budget"] = {
        "timezone": "UTC", "daily_total_tokens": daily, "monthly_total_tokens": monthly
    }
    runtime.user_config_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
