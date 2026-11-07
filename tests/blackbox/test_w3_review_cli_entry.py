"""W1-W3 review: the supported module entry uses the same runtime gate.

The ``python`` test child executes ``src.cli.commands`` with runpy after G0.
Unlike the ``cli`` child, it does not redirect the requested entry to src.main.
Every invocation below starts a fresh process, so a previously wrapped Click
command object cannot hide the native module-entry regression.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.storage.sqlite_store import SQLiteStore
from tests.blackbox.test_w1_cli_parse_regressions import _tree_fingerprint
from tests.offline_runtime import prepare_offline_child_env


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _run(env: dict[str, str], *arguments: str, native: bool) -> subprocess.CompletedProcess[str]:
    target = ("python", "-m", "src.cli.commands") if native else ("cli",)
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tests/offline_entrypoint.py"), *target, *arguments],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )


def _fixture(tmp_path: Path, state: str) -> tuple[dict[str, str], Path]:
    root = tmp_path / "product"
    SQLiteStore(root / "db/knowledge_vault.db").initialize()
    env = prepare_offline_child_env(
        project_root=PROJECT_ROOT,
        runtime_overrides={
            "DATA_DIR": root,
            "DB_PATH": root / "db/knowledge_vault.db",
            "VAULT_DIR": root / "vault",
            "VECTOR_DIR": root / "vectors",
            "LOG_DIR": root / "logs",
            "TMP_DIR": root / "tmp",
        },
    )
    env.pop("PKV_TEST_ABSENT_DATA_ROOT", None)
    # Prewarm the ordinary local cache through the existing synthetic fixture;
    # otherwise its absence masks the missing-snapshot setup state as repair.
    env["PKV_TEST_SYNTHETIC_RUNTIME_READY"] = "1"
    seeded = _run(env, "inspect", native=False)
    assert seeded.returncode == 0, seeded.stdout + seeded.stderr
    assert json.loads(seeded.stdout)["readiness"] == "ready"
    if state != "ready":
        env.pop("PKV_TEST_SYNTHETIC_RUNTIME_READY")
        snapshot = root / "config/local.yaml"
        if state == "damaged":
            snapshot.write_text("runtime_schema_version: [unterminated\n", encoding="utf-8")
        else:
            assert state == "missing"
            snapshot.unlink()
    # Finish harness directory/snapshot setup before measuring product writes.
    inspected = _run(env, "inspect", native=False)
    assert inspected.returncode == 0, inspected.stdout + inspected.stderr
    assert json.loads(inspected.stdout)["readiness"] == {
        "ready": "ready", "missing": "setup_required", "damaged": "repair_required",
    }[state]
    return env, root


@pytest.mark.parametrize("state", ["missing", "damaged"])
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(
            ("outcome", "create-topic", "--title", "Entry gate", "--question", "Is setup complete?"),
            id="v1-mutation",
        ),
        pytest.param(("outcome", "render-v2", "topic-missing"), id="v2-mutation"),
    ],
)
def test_native_module_rejects_unready_mutations_like_standard_entry(
    tmp_path: Path, state: str, arguments: tuple[str, ...],
) -> None:
    env, root = _fixture(tmp_path, state)
    before = _tree_fingerprint(root)
    standard = _run(env, *arguments, native=False)
    native = _run(env, *arguments, native=True)
    assert standard.returncode == native.returncode == 1, native.stdout + native.stderr
    assert standard.stdout == native.stdout == ""
    standard_payload = json.loads(standard.stderr)
    assert json.loads(native.stderr) == standard_payload
    issue = standard_payload["issues"][0] if "issues" in standard_payload else standard_payload
    assert issue["code"] == ("setup_required" if state == "missing" else "repair_required")
    assert issue["stage"] == "runtime_readiness"
    assert _tree_fingerprint(root) == before, "unready module entry wrote product data"


def test_native_module_ready_write_is_visible_to_both_fresh_entries(tmp_path: Path) -> None:
    env, _ = _fixture(tmp_path, "ready")
    created = _run(
        env, "outcome", "create-topic", "--title", "Module entry", "--question", "Was it persisted?",
        native=True,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    topic = json.loads(created.stdout)["result"]
    standard = _run(env, "outcome", "get-v2", topic["topic_id"], native=False)
    native = _run(env, "outcome", "get-v2", topic["topic_id"], native=True)
    assert standard.returncode == native.returncode == 0, native.stdout + native.stderr
    payload = json.loads(native.stdout)
    assert payload == json.loads(standard.stdout)
    assert payload["schema"] == "pkv.lifecycle-response.v2"
    assert payload["result"]["topic"]["topic_id"] == topic["topic_id"]


@pytest.mark.parametrize(
    ("arguments", "exit_code", "fragment"),
    [
        (("outcome", "approve-v2", "--help"), 0, "--expected-revision"),
        (("--version",), 0, "version"),
        (("outcome", "approve-v2", "brief-a", "--expected-revision", "bad"), 2, "Invalid value"),
        (("outcome", "get-v2", "topic-a", "--unknown"), 2, "No such option"),
    ],
)
def test_native_module_parser_precedes_damaged_runtime(
    tmp_path: Path, arguments: tuple[str, ...], exit_code: int, fragment: str,
) -> None:
    env, root = _fixture(tmp_path, "damaged")
    before = _tree_fingerprint(root)
    for native in (False, True):
        result = _run(env, *arguments, native=native)
        assert result.returncode == exit_code, result.stdout + result.stderr
        assert fragment in (result.stdout if exit_code == 0 else result.stderr)
        assert "runtime_readiness" not in result.stdout + result.stderr
        assert _tree_fingerprint(root) == before
