"""Q1-01: persisted patches survive an OS-process boundary before Kernel delete.

The child is the same pytest node, launched through the existing offline pytest
entrypoint.  Its test-only worker branch calls the public Kernel; no product
test mode, Direct-Python allowlist, subprocess guard or network rule is changed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pkv_kernel import Config, ErrorCode, PKVRuntimeError, bootstrap_kernel
from src.application import KnowledgeApplication
from src.runtime.layout import RuntimeLayout
from src.storage.content_lifecycle import ContentMutationState
from tests.offline_runtime import prepare_offline_child_env
from tests.unit.test_w1_q1_regressions import _patch_proof_count, _scenario


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKER_NODE_ENV = "PKV_W2_Q1_WORKER_NODE"
_REQUEST_NAME = "w2-q1-process-request.json"
_RESULT_NAME = "w2-q1-process-result.json"


def _run_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_state: ContentMutationState,
    same_target: bool,
) -> None:
    """Operate only on the root already validated by the offline entrypoint."""
    data_root = Path(os.environ["DATA_DIR"])
    request_path = data_root / "reports" / _REQUEST_NAME
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["parent_pid"] != os.getpid()
    assert payload["initial_state"] == initial_state.value
    assert payload["same_target"] is same_target
    knowledge_id = payload["delete_target"]
    assert type(knowledge_id) is int and knowledge_id > 0

    # _scenario owns this synthetic profile.  This explicit layout never reads
    # the operator's profile or the offline runner's unrelated default graph.
    config = Config(
        layout=RuntimeLayout.resolve(
            resources_root=PROJECT_ROOT,
            user_data_root=data_root,
            profile_root=data_root.parent / "profile",
            environment={},
        )
    )
    provider_calls: list[str] = []

    def forbidden_factory(name: str):
        def construct(*_args: Any, **_kwargs: Any) -> Any:
            provider_calls.append(name)
            raise AssertionError("Q1 process recovery must not construct a Provider")

        return construct

    # These tripwires count unexpected construction; they do not synthesize
    # Provider output or replace the Kernel, queue, coordinator or stores.
    monkeypatch.setattr(
        KnowledgeApplication, "_create_embedder", forbidden_factory("embedding")
    )
    monkeypatch.setattr(
        KnowledgeApplication, "_create_deepseek_client", forbidden_factory("llm")
    )
    kernel = bootstrap_kernel(config)
    errors: list[str] = []
    for _ in range(2):
        try:
            kernel.delete_entry(knowledge_id)
        except PKVRuntimeError as error:
            errors.append(error.code.value)
            assert error.code is ErrorCode.EMBEDDING_PROCESSING
        if kernel.get_entry(knowledge_id) is None:
            break

    deleted = kernel.get_entry(knowledge_id) is None
    result = {
        "child_pid": os.getpid(),
        "parent_pid": payload["parent_pid"],
        "deleted": deleted,
        "provider_calls": provider_calls,
        "errors": errors,
    }
    (data_root / "reports" / _RESULT_NAME).write_text(
        json.dumps(result, sort_keys=True), encoding="utf-8"
    )
    assert provider_calls == []
    assert deleted, (
        "Q1-01: a fresh process could not advance public Kernel delete past the "
        f"persisted {initial_state.value} patch: {errors}"
    )


@pytest.mark.parametrize(
    "initial_state",
    [ContentMutationState.ACCEPTED, ContentMutationState.RETRY_REQUIRED],
    ids=["accepted", "retry_required"],
)
@pytest.mark.parametrize(
    "same_target", [True, False], ids=["same_target", "independent_target"]
)
def test_q1_01_new_process_kernel_delete_after_persisted_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    initial_state: ContentMutationState,
    same_target: bool,
) -> None:
    worker_node = os.environ.get(_WORKER_NODE_ENV)
    if worker_node is not None:
        # An unexpected node cannot recurse or consume another test's request.
        assert worker_node == request.node.nodeid
        _run_worker(
            monkeypatch, initial_state=initial_state, same_target=same_target
        )
        return

    scenario = _scenario(
        tmp_path, monkeypatch, initial_state=initial_state, same_target=same_target
    )
    config = scenario.application.config
    data_root = config.layout.user_data_root
    reports = data_root / "reports"
    reports.mkdir(exist_ok=True)
    payload = {
        "parent_pid": os.getpid(),
        "initial_state": initial_state.value,
        "same_target": same_target,
        "delete_target": scenario.delete_target,
    }
    (reports / _REQUEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    assert _patch_proof_count(scenario) == 0
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is not None

    # The fixture represents a due retry after restart, not a future backoff.
    # Keep that distinction explicit if the low-level retry default changes.
    if initial_state is ContentMutationState.RETRY_REQUIRED:
        with scenario.application._write_lease_scope():
            with sqlite3.connect(config.db_path) as connection:
                connection.execute(
                    "UPDATE content_mutation_tasks SET not_before = CURRENT_TIMESTAMP "
                    "WHERE task_id = ?",
                    (scenario.patch_task.task_id,),
                )

    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": data_root,
            "DB_PATH": config.db_path,
            "VAULT_DIR": config.vault_dir,
            "VECTOR_DIR": config.vector_index_dir,
            "LOG_DIR": config.log_dir,
            "TMP_DIR": config.tmp_dir,
        },
    )
    env[_WORKER_NODE_ENV] = request.node.nodeid
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"),
            "pytest",
            request.node.nodeid,
            "-q",
            "--tb=short",
            f"--basetemp={config.tmp_dir / 'pytest'}",
            "-o",
            f"cache_dir={config.tmp_dir / 'pytest-cache'}",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((reports / _RESULT_NAME).read_text(encoding="utf-8"))
    assert observed["child_pid"] != os.getpid()
    assert observed["parent_pid"] == os.getpid()
    assert observed["deleted"] is True
    assert observed["provider_calls"] == []
    assert scenario.provider_calls == []
    assert scenario.application.sqlite_store.query_by_id(scenario.delete_target) is None

    patch_task = scenario.lifecycle.store.get_task(scenario.patch_task.task_id)
    assert patch_task is not None
    proof_count = _patch_proof_count(scenario)
    assert proof_count <= 1
    if patch_task.state is ContentMutationState.COMPLETED:
        assert proof_count == 1
        if not same_target:
            row = scenario.application.sqlite_store.query_by_id(
                scenario.patch.target_knowledge_id
            )
            assert row is not None and row["summary_100_words"] == scenario.patch.summary
    else:
        # Only deletion of this same target can justify discarding its patch;
        # an independent target must not make a valid patch disappear.
        assert same_target and patch_task.state is ContentMutationState.SUPERSEDED
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM storage_operation_commits "
            "WHERE action = 'delete' AND knowledge_id = ?",
            (scenario.delete_target,),
        ).fetchone()[0] == 1
