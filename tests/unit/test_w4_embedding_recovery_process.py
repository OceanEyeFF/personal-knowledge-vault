"""W4 ER-10/MIG-09: durable completion recovery in independent real workers.

Only owned pytest children are abruptly exited at actual persistence seams.
The successor uses public SDK handles and verifies retained proof/artifacts;
no product environment flag, execution result, Store or proof is fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from pkv_kernel import embedding
from src.runtime import embedding_lifecycle as core
from src.runtime import embedding_rebuild_v2 as lifecycle
from src.runtime.audit import AuditTrace
from src.runtime.embedding_completion_audit import completion_id
from src.runtime.embedding_rebuild_ledger import EmbeddingRebuildLedger
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.utils.config import Config
from tests.offline_runtime import prepare_offline_child_env
from tests.unit.test_embedding_rebuild_ledger import _configured
from tests.unit.test_kernel_embedding_rebuild import _seed_r2


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_NODE = "PKV_W4_RECOVERY_WORKER_NODE"
_REQUEST = "w4-recovery-request.json"
_RESULT = "w4-recovery-result.json"
_METRICS = "w4-recovery-observations.jsonl"
_CRASH_EXIT = 73


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _note(config: Config, kind: str, **facts: object) -> None:
    with (config.layout.user_data_root / "reports" / _METRICS).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"pid": os.getpid(), "kind": kind, **facts}, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _product_fingerprint(config: Config) -> dict[str, str]:
    root = config.layout.user_data_root
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).parts[0] not in {"reports", "tmp"}
    }


def _worker_config(data_root: Path) -> Config:
    return Config(layout=RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT, user_data_root=data_root,
        profile_root=data_root.parent / "profile", environment={},
    ))


def _instrument(config: Config, monkeypatch: pytest.MonkeyPatch, *, phase: str, boundary: str) -> None:
    original_cas = core._pointer_update
    original_replace = EmbeddingRebuildLedger._replace
    original_append = AuditTrace.append
    original_completed = lifecycle.ManualRebuildObserver.mark_completed
    original_completion = lifecycle.append_completion

    def stop() -> None:
        operation, = EmbeddingRebuildLedger(config.layout).read_operations()
        _write_json(config.layout.user_data_root / "reports" / _RESULT, {
            "child_pid": os.getpid(), "boundary": boundary,
            "operation_id": operation.operation_id,
            "reservation_id": operation.reservation.reservation_id,
            "generation_id": operation.generation_id,
            "reservation": operation.reservation.to_dict(),
        })
        os._exit(_CRASH_EXIT)

    def cas(*args: Any, **kwargs: Any) -> Any:
        _note(config, "pointer_cas", phase=phase)
        assert phase == "interrupt", "reconciliation must never repeat pointer CAS"
        result = original_cas(*args, **kwargs)
        if boundary == "pointer_after_commit":
            stop()
        return result

    def replace_record(self: EmbeddingRebuildLedger, operation: Any) -> Any:
        before = self.get_operation(operation.operation_id)
        result = original_replace(self, operation)
        if (before is not None and before.reservation is not None
                and before.reservation.state != "settled" and operation.reservation is not None
                and operation.reservation.state == "settled"):
            _note(config, "settlement", operation_id=operation.operation_id,
                  reservation_id=operation.reservation.reservation_id)
        return result

    def audit_append(self: AuditTrace, event: Any) -> Any:
        result = original_append(self, event)
        if event.get("operation") == "embedding_rebuild" and event.get("phase") == "completed":
            _note(config, "completion_append", completion_id=event["result"]["completion_id"])
            if phase == "interrupt" and boundary == "audit_after_append":
                stop()
        return result

    def audit_completion(*args: Any, **kwargs: Any) -> Any:
        if phase == "interrupt" and boundary == "audit_before_append":
            stop()
        return original_completion(*args, **kwargs)

    def completed(self: Any, operation_id: str) -> Any:
        if phase == "interrupt" and boundary == "ledger_before_completion":
            stop()
        result = original_completed(self, operation_id)
        if phase == "interrupt" and boundary == "ledger_after_completion":
            stop()
        return result

    class EmptyProvider:
        def __init__(self) -> None:
            self.client = self

        def embed_document(self, *_args: Any, **_kwargs: Any) -> Any:
            _note(config, "provider_call")
            raise AssertionError("empty source must never call Provider")

        def embed_batch_numpy(self, *_args: Any, **_kwargs: Any) -> Any:
            _note(config, "provider_call")
            raise AssertionError("empty source must never call Provider")

    def provider_factory(_config: Config) -> Any:
        _note(config, "provider_factory", phase=phase)
        assert phase == "interrupt", "reconciliation must not construct a Provider"
        return EmptyProvider()

    monkeypatch.setattr(core, "_pointer_update", cas)
    monkeypatch.setattr(EmbeddingRebuildLedger, "_replace", replace_record)
    monkeypatch.setattr(AuditTrace, "append", audit_append)
    monkeypatch.setattr(lifecycle, "append_completion", audit_completion)
    monkeypatch.setattr(lifecycle.ManualRebuildObserver, "mark_completed", completed)
    monkeypatch.setattr(lifecycle, "create_embedder", provider_factory)


def _run_worker(monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    data_root = Path(os.environ["DATA_DIR"])
    request = json.loads((data_root / "reports" / _REQUEST).read_text(encoding="utf-8"))
    assert request["parent_pid"] != os.getpid()
    assert request["boundary"] == boundary
    config = _worker_config(data_root)
    phase = request["phase"]
    _instrument(config, monkeypatch, phase=phase, boundary=boundary)
    if phase == "interrupt":
        plan = embedding.plan_embedding_rebuild_v2(embedding.inspect_embedding_rebuild_v2(config))
        embedding.execute_embedding_rebuild_v2(plan, embedding.confirm_embedding_rebuild_v2(
            plan, approved=True, allow_network=True,
        ))
        pytest.fail("worker did not reach its interruption boundary")

    before = _product_fingerprint(config)
    inspection = embedding.inspect_embedding_operation_v2(request["operation_id"], config=config)
    state = inspection.to_dict()
    assert state["effective_state"] == "completed"
    assert state["publication_committed"] is True
    assert state["proof_valid"] is True
    assert state["generation_id"] == request["generation_id"]
    if phase == "reconcile":
        assert state["ledger_completion_pending"] is (boundary != "ledger_after_completion")
        assert state["audit_completion_pending"] is (boundary in {"pointer_after_commit", "audit_before_append"})
    else:
        assert state["ledger_completion_pending"] is False
        assert state["audit_completion_pending"] is False
    result = None
    pending = state["ledger_completion_pending"] or state["audit_completion_pending"]
    if pending:
        assert phase == "reconcile"
        plan = embedding.plan_embedding_publication_v2(inspection)
        assert plan.action == "reconcile_completion"
        assert plan.to_dict()["requires_network"] is False
        assert plan.to_dict()["creates_reservation"] is False
        assert _product_fingerprint(config) == before, "lookup/plan must remain read-only"
        result = embedding.execute_embedding_publication_v2(
            plan, embedding.confirm_embedding_publication_v2(plan, approved=True),
        ).to_dict()
        assert result["provider_called"] is False
        assert result["publication_committed"] is True
        assert result["ledger_completion_pending"] is False
        assert result["audit_completion_pending"] is False
    else:
        with pytest.raises(PKVRuntimeError) as completed:
            embedding.plan_embedding_publication_v2(inspection)
        assert completed.value.code is ErrorCode.EMBEDDING_OPERATION_COMPLETED
        assert _product_fingerprint(config) == before
    final = embedding.inspect_embedding_operation_v2(request["operation_id"], config=config).to_dict()
    assert final["effective_state"] == "completed"
    assert final["ledger_completion_pending"] is False
    assert final["audit_completion_pending"] is False
    _write_json(data_root / "reports" / _RESULT, {
        "child_pid": os.getpid(), "before": state, "after": final, "execution": result,
    })


def _child(config: Config, request: pytest.FixtureRequest, *, boundary: str,
           phase: str, original: dict[str, Any] | None = None) -> dict[str, Any]:
    reports = config.layout.user_data_root / "reports"
    reports.mkdir(exist_ok=True)
    _write_json(reports / _REQUEST, {
        "parent_pid": os.getpid(), "boundary": boundary, "phase": phase,
        **({key: original[key] for key in ("operation_id", "generation_id")} if original else {}),
    })
    env = prepare_offline_child_env(project_root=PROJECT_ROOT, runtime_overrides={
        "DATA_DIR": config.layout.user_data_root, "DB_PATH": config.db_path,
        "VAULT_DIR": config.vault_dir, "VECTOR_DIR": config.vector_index_dir,
        "LOG_DIR": config.log_dir, "TMP_DIR": config.tmp_dir,
    })
    env[_WORKER_NODE] = request.node.nodeid
    completed = subprocess.run([
        sys.executable, str(PROJECT_ROOT / "tests/offline_entrypoint.py"), "pytest", request.node.nodeid,
        "-q", "--tb=short", f"--basetemp={config.tmp_dir / 'pytest'}",
        "-o", f"cache_dir={config.tmp_dir / 'pytest-cache'}",
    ], cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=90, check=False)
    (reports / f"worker-{phase}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
    assert completed.returncode == (_CRASH_EXIT if phase == "interrupt" else 0), completed.stdout + completed.stderr
    result = json.loads((reports / _RESULT).read_text(encoding="utf-8"))
    assert result["child_pid"] != os.getpid()
    return result


@pytest.mark.parametrize("boundary", (
    "pointer_after_commit", "audit_before_append", "audit_after_append",
    "ledger_before_completion", "ledger_after_completion",
))
def test_completion_recovers_after_real_worker_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, boundary: str,
) -> None:
    if os.environ.get(_WORKER_NODE) is not None:
        assert os.environ[_WORKER_NODE] == request.node.nodeid
        _run_worker(monkeypatch, boundary)
        return
    config = _configured(tmp_path)
    _seed_r2(config)
    original = _child(config, request, boundary=boundary, phase="interrupt")
    pointer = (config.layout.user_data_root / "config" / "local.yaml").read_bytes()
    recovered = _child(config, request, boundary=boundary, phase="reconcile", original=original)
    checked = _child(config, request, boundary=boundary, phase="verify", original=original)
    assert len({original["child_pid"], recovered["child_pid"], checked["child_pid"], os.getpid()}) == 4
    assert recovered["after"] == checked["after"]
    assert (config.layout.user_data_root / "config" / "local.yaml").read_bytes() == pointer
    operation, = EmbeddingRebuildLedger(config.layout).read_operations()
    assert operation.operation_id == original["operation_id"]
    assert operation.reservation.reservation_id == original["reservation_id"]
    assert operation.reservation.state == "settled"
    assert operation.reservation.reserved_tokens == original["reservation"]["reserved_tokens"]
    assert operation.reservation.settled_tokens == operation.reservation.reserved_tokens
    observations = [json.loads(line) for line in (config.layout.user_data_root / "reports" / _METRICS).read_text(encoding="utf-8").splitlines()]
    assert [item["kind"] for item in observations].count("pointer_cas") == 1
    assert [item["kind"] for item in observations].count("provider_factory") == 1
    assert [item["kind"] for item in observations].count("provider_call") == 0
    assert [item["kind"] for item in observations].count("settlement") == 1
    assert [item["kind"] for item in observations].count("completion_append") == 1
    events = [json.loads(line)["event"] for line in (config.log_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    completion, = [event for event in events if event.get("operation") == "embedding_rebuild" and event.get("phase") == "completed"]
    identity = completion["result"]
    assert identity["completion_id"] == completion_id(
        operation_id=operation.operation_id, generation_id=operation.generation_id,
        manifest_sha256=identity["manifest_sha256"],
    )
