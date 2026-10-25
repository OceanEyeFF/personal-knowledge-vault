"""W1/R-08: nested CLI parsing must precede any runtime readiness gate.

The original W1 red assertions cover absent/READY roots through real offline
CLI children. W2 adds a damaged runtime snapshot and valid leaf invocations.
The malformed user-profile YAML case is separate: the G0 harness parses that
input before the product parser, so this suite does not claim to cover it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage.sqlite_store import SQLiteStore
from tests.offline_runtime import prepare_offline_child_env


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(env: dict[str, str], arguments: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tests" / "offline_entrypoint.py"), "cli", *arguments],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _tree_fingerprint(root: Path) -> dict[str, str]:
    """Compare only this test's data root, including directory creation."""

    if not os.path.lexists(root):
        return {}
    result = {".": "directory"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        result[relative] = (
            "directory" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
    return result


@pytest.fixture(
    params=("absent", "damaged", "ready"),
    ids=("unready-absent-root", "damaged-runtime-snapshot", "ready-control"),
)
def cli_runtime(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[dict[str, str], Path, str]:
    """Use existing entrypoint isolation seams without weakening G0."""

    data_root = tmp_path / "product"
    db_path = data_root / "db" / "knowledge_vault.db"
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": data_root,
            "DB_PATH": db_path,
            "VAULT_DIR": data_root / "vault",
            "VECTOR_DIR": data_root / "vectors",
            "LOG_DIR": data_root / "logs",
            "TMP_DIR": data_root / "tmp",
        },
    )
    env.pop("PKV_TEST_SYNTHETIC_RUNTIME_READY", None)
    env.pop("PKV_TEST_ABSENT_DATA_ROOT", None)
    if request.param == "absent":
        env["PKV_TEST_ABSENT_DATA_ROOT"] = "1"
        assert not os.path.lexists(data_root)
    else:
        db_path.parent.mkdir(parents=True)
        SQLiteStore(db_path).initialize()
        if request.param == "ready":
            env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"
        else:
            snapshot_path = data_root / "config" / "local.yaml"
            snapshot_path.parent.mkdir(parents=True)
            snapshot_path.write_text("runtime_schema_version: [unterminated\n", encoding="utf-8")

    # Establish the actual state through a public command. The READY fixture's
    # allowed setup is complete before measuring parse-only side effects.
    inspected = _run_cli(env, ("inspect",))
    assert inspected.returncode == 0, inspected.stderr
    expected_readiness = {
        "absent": "setup_required", "damaged": "repair_required", "ready": "ready"
    }[request.param]
    payload = json.loads(inspected.stdout)
    assert payload["readiness"] == expected_readiness
    if request.param == "damaged":
        assert any(
            issue["code"] == "runtime_snapshot_invalid"
            for issue in payload["inspection"]["issues"]
        )
    if request.param == "absent":
        assert not os.path.lexists(data_root)
    return env, data_root, request.param


@pytest.mark.parametrize(
    ("arguments", "expected_exit", "expected_fragment"),
    [
        pytest.param(("outcome", "--help"), 0, "Commands:", id="CLI-01-group-help"),
        pytest.param(("outcome", "get", "--help"), 0, "TOPIC_ID", id="CLI-01-read-leaf-help"),
        pytest.param(
            ("outcome", "create-topic", "--help"),
            0,
            "--question",
            id="CLI-01-mutation-leaf-help",
        ),
        pytest.param(("--version",), 0, "version", id="CLI-01-root-version"),
        pytest.param(
            ("outcome", "get", "--version"),
            2,
            "No such option",
            id="CLI-01-unsupported-leaf-version",
        ),
        pytest.param(
            ("outcome", "get"),
            2,
            "Missing argument 'TOPIC_ID'",
            id="CLI-02-missing-required-argument",
        ),
        pytest.param(
            ("outcome", "list", "--limit", "invalid"),
            2,
            "Invalid value for '--limit'",
            id="CLI-02-invalid-integer-option",
        ),
        pytest.param(
            ("outcome", "list", "--w1-unknown-option"),
            2,
            "No such option",
            id="CLI-02-unknown-leaf-option",
        ),
    ],
)
def test_outcome_parse_precedes_runtime_gate(
    cli_runtime: tuple[dict[str, str], Path, str],
    arguments: tuple[str, ...],
    expected_exit: int,
    expected_fragment: str,
) -> None:
    """CLI-04: readiness cannot replace eager help or Click errors."""

    env, data_root, _ = cli_runtime
    before = _tree_fingerprint(data_root)
    result = _run_cli(env, arguments)
    after = _tree_fingerprint(data_root)

    assert after == before, "A parse-only CLI request changed its isolated data root"
    assert result.returncode == expected_exit, (
        f"{arguments!r}: expected Click exit {expected_exit}, got {result.returncode}; "
        f"stdout={result.stdout!r}; stderr={result.stderr!r}"
    )
    output = result.stdout if expected_exit == 0 else result.stderr
    assert expected_fragment in output
    assert "runtime_readiness" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(("outcome", "list"), id="CLI-03-read-leaf"),
        pytest.param(
            ("outcome", "create-topic", "--title", "Synthetic topic", "--question", "What changed?"),
            id="CLI-03-mutation-leaf",
        ),
    ],
)
def test_valid_outcome_leaf_observes_runtime_readiness(
    cli_runtime: tuple[dict[str, str], Path, str], arguments: tuple[str, ...]
) -> None:
    """Valid leaves enter the runtime gate only after all parsing succeeds."""

    env, data_root, state = cli_runtime
    before = _tree_fingerprint(data_root)
    result = _run_cli(env, arguments)
    if state == "ready":
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["status"] == "success"
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        payload = json.loads(result.stderr)
        assert payload["code"] == (
            "setup_required" if state == "absent" else "repair_required"
        )
        assert payload["stage"] == "runtime_readiness"
        assert _tree_fingerprint(data_root) == before
