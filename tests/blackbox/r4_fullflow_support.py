"""Shared process-boundary support for R4 source black-box acceptance tests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Iterable

from mcp import StdioServerParameters

from src.runtime.ai_automation_policy import (
    AutomationPolicyState,
    inspect_ai_automation_policy,
)
from src.runtime.bootstrap import bootstrap_runtime
from src.runtime.embedding_lifecycle import (
    EmbeddingIndexState,
    SQLiteEmbeddingSource,
    inspect_embedding_index,
    resolve_embedding_index_binding,
)
from src.runtime.layout import RuntimeLayout
from src.runtime.write_lease import write_lease_scope
from src.storage.markdown_store import MarkdownStore
from src.storage.derivation_patch import DerivationPatchReference, DerivationPatchSpool
from src.storage.sqlite_store import (
    SQLiteStore,
    entry_projection_sha256,
    row_projection_sha256,
)
from src.utils.config import Config
from tests.offline_runtime import prepare_offline_child_env


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OFFLINE_ENTRYPOINT = PROJECT_ROOT / "tests" / "offline_entrypoint.py"
PROVIDER_HARNESS = (
    PROJECT_ROOT / "tests" / "harness" / "r4_openai_compatible_provider.py"
)
BLACKBOX_API_KEY = "pkv-blackbox-not-a-secret"
BLACKBOX_LLM_MODEL = "pkv-r4-blackbox-chat-v1"
BLACKBOX_EMBEDDING_MODEL = "pkv-r4-blackbox-embedding-v1"
BLACKBOX_EMBEDDING_DIM = 3


def _success_provider_script(scenario_id: str) -> dict[str, Any]:
    vector = [0.26726124, 0.53452248, 0.80178373]

    def chat(
        step_id: str,
        content: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "endpoint": "/v1/chat/completions",
            "barrier": True,
            "response": {
                "kind": "success",
                "content": content,
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
            },
        }

    def embedding(step_id: str, *, prompt_tokens: int) -> dict[str, Any]:
        return {
            "step_id": step_id,
            "endpoint": "/v1/embeddings",
            "barrier": True,
            "response": {
                "kind": "success",
                "vectors": [vector],
                "usage": {"prompt_tokens": prompt_tokens},
            },
        }

    return {
        "schema_version": "pkv.r4.openai-compatible-script.v1",
        "scenario_id": scenario_id,
        "chat_model": BLACKBOX_LLM_MODEL,
        "embedding_model": BLACKBOX_EMBEDDING_MODEL,
        "embedding_dimension": BLACKBOX_EMBEDDING_DIM,
        "barrier_timeout_seconds": 30,
        "steps": [
            chat(
                "chat-1",
                '["r4","blackbox","semantic"]',
                prompt_tokens=11,
                completion_tokens=5,
            ),
            chat(
                "chat-2",
                '["r4","blackbox","semantic"]',
                prompt_tokens=11,
                completion_tokens=5,
            ),
            embedding("embedding-archive-1", prompt_tokens=13),
            embedding("embedding-archive-2", prompt_tokens=13),
            embedding("embedding-query-1", prompt_tokens=13),
            embedding("embedding-query-2", prompt_tokens=13),
        ],
    }


class ProviderHarness:
    """Controller for the process-external deterministic Provider."""

    def __init__(
        self,
        *,
        root: Path,
        scenario_id: str,
        process: subprocess.Popen[str],
        events: Queue[dict[str, Any]],
        reader: threading.Thread,
    ) -> None:
        self.root = root
        self.scenario_id = scenario_id
        self.process = process
        self._events = events
        self._reader = reader
        self.base_url = ""
        self.observed_events: list[dict[str, Any]] = []
        self._closed = False

    @classmethod
    def start(cls, root: Path, *, scenario_id: str) -> "ProviderHarness":
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=False)
        script_path = root / "script.json"
        state_dir = root / "state"
        script_path.write_text(
            json.dumps(
                _success_provider_script(scenario_id),
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        provider_data = root / "runtime"
        provider_layout = RuntimeLayout.resolve(
            resources_root=PROJECT_ROOT,
            user_data_root=provider_data,
            profile_root=root / "profile" / ".pkv",
            environment={},
        )
        env = prepare_offline_child_env(
            project_root=PROJECT_ROOT,
            runtime_overrides={
                "DATA_DIR": provider_layout.user_data_root,
                "DB_PATH": provider_layout.db_path,
                "VAULT_DIR": provider_layout.vault_dir,
                "VECTOR_DIR": provider_layout.vector_index_dir,
                "LOG_DIR": provider_layout.log_dir,
                "TMP_DIR": provider_layout.tmp_dir,
                "LOG_LEVEL": "WARNING",
            },
        )
        command = [
            sys.executable,
            str(OFFLINE_ENTRYPOINT),
            "python",
            str(PROVIDER_HARNESS),
            "--script",
            str(script_path),
            "--state-dir",
            str(state_dir),
        ]
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        events: Queue[dict[str, Any]] = Queue()

        def read_events() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    events.put({"event": "invalid_stdout"})
                    continue
                events.put(
                    event if type(event) is dict else {"event": "invalid_stdout"}
                )
            events.put({"event": "stdout_eof"})

        reader = threading.Thread(
            target=read_events,
            name=f"r4-provider-events-{scenario_id}",
            daemon=True,
        )
        reader.start()
        controller = cls(
            root=root,
            scenario_id=scenario_id,
            process=process,
            events=events,
            reader=reader,
        )
        ready = controller.await_event("ready", timeout=20)
        if (
            ready.get("port") is None
            or ready.get("base_url") != f"http://127.0.0.1:{ready['port']}/v1"
            or ready.get("expected_step_count") != 6
        ):
            controller.close(expect_completed=False)
            raise AssertionError(f"invalid Provider ready event: {ready!r}")
        controller.base_url = str(ready["base_url"])
        return controller

    def await_event(
        self,
        event_name: str,
        *,
        step_id: str | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"Provider event timed out: event={event_name} step={step_id}"
                )
            try:
                event = self._events.get(timeout=remaining)
            except Empty as exc:
                raise AssertionError(
                    f"Provider event timed out: event={event_name} step={step_id}"
                ) from exc
            self.observed_events.append(event)
            actual_name = event.get("event")
            if actual_name in {"harness_failed", "startup_failed", "invalid_stdout"}:
                raise AssertionError(f"Provider harness failed: {event!r}")
            if actual_name == "stdout_eof":
                raise AssertionError(
                    f"Provider harness exited before {event_name}/{step_id}"
                )
            if actual_name == event_name and (
                step_id is None or event.get("step_id") == step_id
            ):
                return event

    def await_request(self, step_id: str, *, timeout: float = 30) -> dict[str, Any]:
        return self.await_event("request_waiting", step_id=step_id, timeout=timeout)

    def continue_request(self, step_id: str) -> None:
        if self.process.stdin is None:
            raise AssertionError("Provider harness stdin is unavailable")
        self.process.stdin.write(f"CONTINUE {step_id}\n")
        self.process.stdin.flush()

    def assert_redacted_telemetry(self, *private_values: str) -> None:
        telemetry_path = self.root / "state" / "telemetry.ndjson"
        raw = telemetry_path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in raw.splitlines()]
        _assert_private_values_absent(
            raw,
            records,
            (*private_values, BLACKBOX_API_KEY, str(self.root)),
        )
        requests = [
            record for record in records if record["event"] == "request_waiting"
        ]
        assert {record["step_id"] for record in requests} == {
            "chat-1",
            "chat-2",
            "embedding-archive-1",
            "embedding-archive-2",
            "embedding-query-1",
            "embedding-query-2",
        }
        assert len(requests) == 6
        assert all("request_sha256" in record for record in requests)
        assert all("content_sha256" in record for record in requests)

    def close(self, *, expect_completed: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process.poll() is None and self.process.stdin is not None:
            try:
                self.process.stdin.write("SHUTDOWN\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            return_code = self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                return_code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                return_code = self.process.wait(timeout=5)
        self._reader.join(timeout=5)
        stderr = self.process.stderr.read() if self.process.stderr is not None else ""
        if expect_completed:
            result = json.loads(
                (self.root / "state" / "result.json").read_text("utf-8")
            )
            assert return_code == 0, f"Provider harness exit={return_code}"
            assert stderr == "", "Provider harness wrote unexpected stderr"
            assert result == {
                "completed_step_count": 6,
                "completed_step_ids": [
                    "chat-1",
                    "chat-2",
                    "embedding-archive-1",
                    "embedding-archive-2",
                    "embedding-query-1",
                    "embedding-query-2",
                ],
                "expected_step_count": 6,
                "harness_version": "1.0.0",
                "outcome": "completed",
                "request_count": 6,
                "scenario_id": self.scenario_id,
                "schema_version": "pkv.r4.openai-compatible-result.v1",
            }

    def __enter__(self) -> "ProviderHarness":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close(expect_completed=exc_type is None)


@dataclass(frozen=True)
class R4BlackboxRuntime:
    """One test-owned product runtime and its normal synthetic user config."""

    root: Path
    layout: RuntimeLayout
    config: Config
    user_config_path: Path

    @classmethod
    def create(cls, root: Path, *, provider_base_url: str) -> "R4BlackboxRuntime":
        root = root.resolve()
        layout = RuntimeLayout.resolve(
            resources_root=PROJECT_ROOT,
            user_data_root=root,
            profile_root=root / "profile" / ".pkv",
            environment={},
        )
        layout.user_config_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "ai": {
                "llm": {
                    "provider": "openai_compatible",
                    "api_key": BLACKBOX_API_KEY,
                    "base_url": provider_base_url,
                    "model": BLACKBOX_LLM_MODEL,
                    "timeout_seconds": 10,
                    "max_retries": 0,
                },
                "embedding": {
                    "provider": "openai_compatible",
                    "api_key": BLACKBOX_API_KEY,
                    "base_url": provider_base_url,
                    "model": BLACKBOX_EMBEDDING_MODEL,
                    "dim": BLACKBOX_EMBEDDING_DIM,
                    "timeout_seconds": 10,
                    "max_retries": 0,
                },
                "automation": {
                    "schema_version": 1,
                    "enabled": True,
                    "authorization": {"policy_sha256": None},
                    "token_budget": {
                        "timezone": "UTC",
                        "daily_total_tokens": 100_000,
                        "monthly_total_tokens": 1_000_000,
                    },
                    "retry": {"max_attempts": 2},
                },
            }
        }
        layout.user_config_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        pending = Config(layout=layout)
        policy = inspect_ai_automation_policy(pending)
        if (
            policy.state is not AutomationPolicyState.AUTHORIZATION_REQUIRED
            or policy.policy_fingerprint is None
        ):
            raise AssertionError(
                f"unexpected pending automation policy: {policy.state}"
            )
        payload["ai"]["automation"]["authorization"] = {
            "policy_sha256": policy.policy_fingerprint
        }
        layout.user_config_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        config = Config(layout=layout)
        ready_policy = inspect_ai_automation_policy(config)
        if ready_policy.state is not AutomationPolicyState.READY:
            raise AssertionError(
                f"synthetic automation policy did not become ready: {ready_policy.state}"
            )
        bootstrap_runtime(config)
        return cls(root, layout, config, layout.user_config_path)

    def child_env(self) -> dict[str, str]:
        env = prepare_offline_child_env(
            project_root=PROJECT_ROOT,
            runtime_overrides={
                "DATA_DIR": self.layout.user_data_root,
                "DB_PATH": self.layout.db_path,
                "VAULT_DIR": self.layout.vault_dir,
                "VECTOR_DIR": self.layout.vector_index_dir,
                "LOG_DIR": self.layout.log_dir,
                "TMP_DIR": self.layout.tmp_dir,
                "LOG_LEVEL": "WARNING",
            },
        )
        profile = self.layout.profile_root.parent
        env.update(
            {
                "HOME": str(profile),
                "USERPROFILE": str(profile),
                "APPDATA": str(profile / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(profile / "AppData" / "Local"),
                "XDG_CONFIG_HOME": str(profile / ".config"),
                "XDG_CACHE_HOME": str(profile / ".cache"),
                "XDG_DATA_HOME": str(profile / ".local" / "share"),
                "PKV_TEST_SYNTHETIC_RUNTIME_READY": "1",
                "PKV_TEST_USER_CONFIG": str(self.user_config_path),
            }
        )
        return env

    def cli_command(self, *arguments: str) -> list[str]:
        return [
            sys.executable,
            str(OFFLINE_ENTRYPOINT),
            "cli",
            *arguments,
        ]

    def start_cli(self, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.cli_command(*arguments),
            cwd=PROJECT_ROOT,
            env=self.child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def run_cli(
        self,
        *arguments: str,
        timeout: float = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.cli_command(*arguments),
            cwd=PROJECT_ROOT,
            env=self.child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def mcp_server_params(self) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=[str(OFFLINE_ENTRYPOINT), "mcp"],
            env=self.child_env(),
            cwd=str(PROJECT_ROOT),
        )

    def prove_writer_lease_is_free(self) -> None:
        """Acquire the OS writer lease while a Provider request is held."""

        with write_lease_scope(self.layout):
            pass


def parse_mcp_tool_json(result: object) -> dict[str, Any]:
    if getattr(result, "isError", False):
        raise AssertionError(f"MCP tool returned error content: {result!r}")
    content = getattr(result, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise AssertionError(f"MCP tool did not return one content block: {result!r}")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str) or not text:
        raise AssertionError("MCP tool did not return JSON text")
    payload = json.loads(text)
    if type(payload) is not dict:
        raise AssertionError("MCP tool JSON is not an object")
    return payload


def _one(rows: Iterable[sqlite3.Row], *, label: str) -> sqlite3.Row:
    copied = list(rows)
    if len(copied) != 1:
        raise AssertionError(f"expected one {label}, got {len(copied)}")
    return copied[0]


def operation_id_for_knowledge(runtime: R4BlackboxRuntime, knowledge_id: int) -> str:
    with sqlite3.connect(runtime.layout.db_path) as connection:
        rows = connection.execute(
            "SELECT operation_id FROM storage_operation_commits "
            "WHERE action = 'archive' AND knowledge_id = ?",
            (knowledge_id,),
        ).fetchall()
    return str(_one(rows, label="archive operation")[0])


def assert_r4_success_ledger(
    runtime: R4BlackboxRuntime,
    *,
    operation_id: str,
    knowledge_id: int,
    expected_title: str,
    expected_content: str,
) -> dict[str, Any]:
    """Check durable facts by exact public operation/task identity."""

    with sqlite3.connect(runtime.layout.db_path) as connection:
        connection.row_factory = sqlite3.Row
        ingress = _one(
            connection.execute(
                "SELECT task_id, state FROM ingress_tasks WHERE operation_id = ?",
                (operation_id,),
            ),
            label="Q0 task",
        )
        mutation = _one(
            connection.execute(
                "SELECT task_id, state, target_knowledge_id, target_revision_sha256 "
                "FROM content_mutation_tasks WHERE operation_id = ?",
                (operation_id,),
            ),
            label="Q1 task",
        )
        handoff = _one(
            connection.execute(
                "SELECT derivation_task_id, state, source_digest, binding_state "
                "FROM content_ai_handoffs WHERE operation_id = ?",
                (operation_id,),
            ),
            label="Q1/Q2 handoff",
        )
        derivation = _one(
            connection.execute(
                "SELECT task_id, state, target_knowledge_id, target_revision_sha256, "
                "patch_ref, patch_sha256, patch_applied, source_digest "
                "FROM ai_derivation_tasks "
                "WHERE operation_id = ?",
                (operation_id,),
            ),
            label="Q2 task",
        )
        reservations = connection.execute(
            "SELECT reservation_id, state, reserved_tokens, reserved_micros, "
            "settled_tokens, settled_micros, currency "
            "FROM ai_derivation_reservations WHERE task_id = ?",
            (derivation["task_id"],),
        ).fetchall()
        usage = connection.execute(
            "SELECT reservation_id, stage, source, uncached_input_tokens, "
            "cached_input_tokens, generated_tokens, embedding_input_tokens, "
            "amount_micros, currency "
            "FROM ai_derivation_usage WHERE task_id = ?",
            (derivation["task_id"],),
        ).fetchall()
        patch_tasks = connection.execute(
            "SELECT operation_id, action, state, patch_ref, patch_sha256, "
            "target_knowledge_id, target_revision_sha256 "
            "FROM content_mutation_tasks WHERE patch_ref = ?",
            (derivation["patch_ref"],),
        ).fetchall()
        patch_task = _one(patch_tasks, label="Q1 patch task")
        patch_commits = connection.execute(
            "SELECT operation_id, action, knowledge_id, "
            "previous_revision_sha256, resulting_revision_sha256, "
            "relative_file_path FROM r4_content_operation_commits "
            "WHERE operation_id = ?",
            (patch_task["operation_id"],),
        ).fetchall()

    assert ingress["state"] == "submitted"
    assert mutation["state"] == "completed"
    assert mutation["target_knowledge_id"] == knowledge_id
    assert handoff["state"] == "q2_activated"
    assert handoff["derivation_task_id"] == derivation["task_id"]
    assert handoff["source_digest"] == derivation["source_digest"]
    assert handoff["binding_state"] == "processing"
    assert derivation["state"] == "completed"
    assert derivation["target_knowledge_id"] == knowledge_id
    assert derivation["target_revision_sha256"] == mutation["target_revision_sha256"]
    assert isinstance(derivation["patch_ref"], str) and derivation["patch_ref"]
    assert isinstance(derivation["patch_sha256"], str)
    assert len(derivation["patch_sha256"]) == 64
    assert derivation["patch_applied"] == 1

    assert len(reservations) == 1
    reservation = reservations[0]
    assert reservation["state"] == "settled"
    assert reservation["reserved_tokens"] == reservation["settled_tokens"]
    assert reservation["reserved_micros"] is None
    assert reservation["settled_micros"] is None
    assert reservation["currency"] is None

    sqlite_store = SQLiteStore(runtime.layout.db_path, runtime_config=runtime.config)
    row = sqlite_store.query_by_id(knowledge_id)
    assert row is not None
    chunks = sqlite_store.get_chunks_by_knowledge_id(knowledge_id)
    estimated_llm_input = math.ceil(len(expected_content) / 4)
    estimated_embedding_input = math.ceil(
        (len(expected_content) + sum(len(str(chunk["chunk_text"])) for chunk in chunks))
        / 4
    )
    expected_reserved_tokens = (
        (estimated_llm_input * 2) + 600 + 200 + estimated_embedding_input
    )
    assert reservation["reserved_tokens"] == expected_reserved_tokens
    assert reservation["settled_tokens"] == expected_reserved_tokens
    assert expected_reserved_tokens > 0

    assert len(usage) == 6
    assert all(row["reservation_id"] == reservation["reservation_id"] for row in usage)
    usage_by_identity = {(row["stage"], row["source"]): row for row in usage}
    assert len(usage_by_identity) == len(usage)
    provider_usage_rows = [row for row in usage if row["source"] == "provider_reported"]
    assert len(provider_usage_rows) == 3
    provider_usage = {row["stage"]: row for row in provider_usage_rows}
    assert set(provider_usage) == {"summary", "tags", "embedding"}
    expected_provider_usage = {
        "summary": (11, None, 5, None, None, None),
        "tags": (11, None, 5, None, None, None),
        "embedding": (None, None, None, 26, None, None),
    }
    for stage, expected in expected_provider_usage.items():
        usage_row = provider_usage[stage]
        assert usage_row["reservation_id"] == reservation["reservation_id"]
        assert (
            usage_row["uncached_input_tokens"],
            usage_row["cached_input_tokens"],
            usage_row["generated_tokens"],
            usage_row["embedding_input_tokens"],
            usage_row["amount_micros"],
            usage_row["currency"],
        ) == expected

    expected_estimated_usage = {
        "summary": (estimated_llm_input, None, 600, None, None, None),
        "tags": (estimated_llm_input, None, 200, None, None, None),
        "embedding": (None, None, None, estimated_embedding_input, None, None),
    }
    for stage, expected in expected_estimated_usage.items():
        estimated = usage_by_identity[(stage, "local_estimate")]
        assert (
            estimated["uncached_input_tokens"],
            estimated["cached_input_tokens"],
            estimated["generated_tokens"],
            estimated["embedding_input_tokens"],
            estimated["amount_micros"],
            estimated["currency"],
        ) == expected

    patch_commit = _one(patch_commits, label="Q2 patch commit")
    assert patch_task["action"] == "apply_ai_patch"
    assert patch_task["state"] == "completed"
    assert patch_task["patch_ref"] == derivation["patch_ref"]
    assert isinstance(patch_task["patch_sha256"], str)
    assert len(patch_task["patch_sha256"]) == 64
    assert patch_task["patch_sha256"] == derivation["patch_sha256"]
    assert patch_task["target_knowledge_id"] == knowledge_id
    assert patch_task["target_revision_sha256"] == mutation["target_revision_sha256"]
    assert patch_commit["operation_id"] == patch_task["operation_id"]
    assert patch_commit["action"] == "apply_ai_patch"
    assert patch_commit["knowledge_id"] == knowledge_id
    patch = DerivationPatchSpool(runtime.layout).read(
        DerivationPatchReference(
            patch_id=derivation["patch_ref"],
            payload_sha256=derivation["patch_sha256"],
        )
    )
    assert patch.patch_id == derivation["patch_ref"]
    assert patch.derivation_task_id == derivation["task_id"]
    assert patch.target_knowledge_id == knowledge_id
    assert patch.expected_revision_sha256 == mutation["target_revision_sha256"]
    assert patch.input_digest == derivation["source_digest"]
    assert (
        patch_commit["previous_revision_sha256"] == mutation["target_revision_sha256"]
    )
    current_revision = row_projection_sha256(row, chunks)
    assert patch_commit["resulting_revision_sha256"] == current_revision
    assert row["title"] == expected_title
    assert row["content"] == expected_content
    assert row["summary_100_words"] == '["r4","blackbox","semantic"]'
    row_tags = (
        row["tags"]
        if isinstance(row["tags"], list)
        else [tag.strip() for tag in str(row["tags"]).split(",") if tag.strip()]
    )
    assert row_tags == ["r4", "blackbox", "semantic"]
    markdown = MarkdownStore(runtime.layout.vault_dir, create=False).load(
        patch_commit["relative_file_path"]
    )
    assert markdown.title == row["title"]
    assert markdown.content == row["content"]
    assert markdown.summary_100_words == row["summary_100_words"]
    assert markdown.tags == row_tags
    assert (
        entry_projection_sha256(
            markdown,
            patch_commit["relative_file_path"],
            [str(chunk["chunk_text"]) for chunk in chunks],
        )
        == current_revision
    )

    inspection = inspect_embedding_index(
        runtime.config,
        source=SQLiteEmbeddingSource(),
    )
    assert inspection.state is EmbeddingIndexState.READY
    assert inspection.active_generation is not None
    binding = resolve_embedding_index_binding(runtime.config)
    assert binding.generation_id == inspection.active_generation
    assert binding.index_dir == (
        runtime.layout.vector_index_dir / "generations" / binding.generation_id
    )
    assert (binding.index_dir / "generation-manifest.json").is_file()
    vector_root_children = list(runtime.layout.vector_index_dir.iterdir())
    assert {path.name for path in vector_root_children} == {"generations", "staging"}
    for lifecycle_root in vector_root_children:
        lifecycle_stat = os.lstat(lifecycle_root)
        assert stat.S_ISDIR(lifecycle_stat.st_mode)
        assert not stat.S_ISLNK(lifecycle_stat.st_mode)
        assert not bool(
            getattr(lifecycle_stat, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )
    staging_root = runtime.layout.vector_index_dir / "staging"
    assert list(staging_root.iterdir()) == []
    return {
        "q0_task_id": ingress["task_id"],
        "q1_task_id": mutation["task_id"],
        "q2_task_id": derivation["task_id"],
        "generation_id": binding.generation_id,
    }


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif type(value) is dict:
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _string_values(item)
    elif type(value) in {list, tuple}:
        for item in value:
            yield from _string_values(item)


def _private_variants(value: str) -> set[str]:
    if not value:
        return set()
    return {
        value,
        value.replace("\\", "/"),
        value.replace("/", "\\"),
        json.dumps(value, ensure_ascii=False)[1:-1],
    }


def _assert_private_values_absent(
    raw_text: str,
    parsed_values: Iterable[Any],
    private_values: Iterable[str],
) -> None:
    variants = {
        variant
        for private_value in private_values
        for variant in _private_variants(private_value)
    }
    if any(variant in raw_text for variant in variants):
        raise AssertionError("observed output contained a private value")
    normalized_private = {variant.replace("\\", "/") for variant in variants if variant}
    for parsed in parsed_values:
        for observed in _string_values(parsed):
            normalized_observed = observed.replace("\\", "/")
            if any(value in normalized_observed for value in normalized_private):
                raise AssertionError("parsed output contained a private value")


def assert_no_public_leak(
    payload_text: str,
    runtime: R4BlackboxRuntime,
    *private_values: str,
) -> None:
    parsed: list[Any] = []
    try:
        parsed.append(json.loads(payload_text))
    except json.JSONDecodeError:
        for line in payload_text.splitlines():
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    _assert_private_values_absent(
        payload_text,
        parsed,
        (*private_values, BLACKBOX_API_KEY, str(runtime.root)),
    )


def finish_process(
    process: subprocess.Popen[str],
    *,
    timeout: float = 30,
) -> tuple[str, str]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        stdout_bytes = stdout.encode("utf-8", errors="replace")
        stderr_bytes = stderr.encode("utf-8", errors="replace")
        raise AssertionError(
            "product child timed out; "
            f"stdout_bytes={len(stdout_bytes)} "
            f"stdout_sha256={hashlib.sha256(stdout_bytes).hexdigest()} "
            f"stderr_bytes={len(stderr_bytes)} "
            f"stderr_sha256={hashlib.sha256(stderr_bytes).hexdigest()}"
        )
    return stdout, stderr


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


__all__ = [
    "BLACKBOX_API_KEY",
    "BLACKBOX_EMBEDDING_DIM",
    "BLACKBOX_EMBEDDING_MODEL",
    "BLACKBOX_LLM_MODEL",
    "OFFLINE_ENTRYPOINT",
    "PROJECT_ROOT",
    "ProviderHarness",
    "R4BlackboxRuntime",
    "assert_no_public_leak",
    "assert_r4_success_ledger",
    "finish_process",
    "operation_id_for_knowledge",
    "parse_mcp_tool_json",
    "stop_process",
]
