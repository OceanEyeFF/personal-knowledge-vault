"""True public-CLI, process-boundary foreground Embedding rebuild acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.runtime.embedding_rebuild_ledger import (
    EmbeddingRebuildLedger,
    EmbeddingRebuildLedgerState,
)
from src.runtime.runtime_snapshot import RuntimeSnapshotStore
from src.runtime.write_lease import write_lease_scope
from src.storage.markdown_store import Entry, MarkdownStore
from src.storage.sqlite_store import SQLiteStore
from tests.blackbox.r4_fullflow_support import (
    ProviderHarness,
    R4BlackboxRuntime,
    archive_only_provider_script,
    assert_no_public_leak,
    assert_r4_success_ledger,
    finish_process,
    manual_embedding_rebuild_provider_script,
    operation_id_for_knowledge,
    stop_process,
)


ARCHIVE_TITLE = "R4 P1 public CLI rebuild"
ARCHIVE_CONTENT = (
    "Larch signals keep a durable semantic route through a process restart and "
    "a public Embedding generation rebuild."
)
SEARCH_QUERY = "durable semantic route after rebuild"


def _parse_json(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    assert type(payload) is dict
    return payload


def _release_step(
    harness: ProviderHarness,
    runtime: R4BlackboxRuntime,
    step_id: str,
) -> None:
    request = harness.await_request(step_id)
    assert request["request_number"] >= 1
    runtime.prove_writer_lease_is_free()
    harness.continue_request(step_id)
    harness.await_event("step_completed", step_id=step_id)


@pytest.mark.slow
def test_public_cli_force_rebuild_settles_ledger_and_survives_restart(
    tmp_path: Path,
) -> None:
    """A real CLI Provider rebuild has no root lease and activates a new generation."""

    scenario_id = "cli-r4p1-public-rebuild"
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=manual_embedding_rebuild_provider_script(scenario_id),
    ) as harness:
        runtime = R4BlackboxRuntime.create(
            tmp_path / "product",
            provider_base_url=harness.base_url,
        )

        archive = runtime.start_cli(
            "archive-text",
            ARCHIVE_CONTENT,
            "--title",
            ARCHIVE_TITLE,
            "--format",
            "json",
        )
        try:
            for step_id in (
                "chat-1",
                "chat-2",
                "embedding-archive-1",
                "embedding-archive-2",
            ):
                _release_step(harness, runtime, step_id)
            archive_stdout, archive_stderr = finish_process(archive)
        except BaseException:
            stop_process(archive)
            raise

        assert archive.returncode == 0
        archive_payload = _parse_json(archive_stdout)
        knowledge_id = archive_payload.get("knowledge_id")
        assert type(knowledge_id) is int and knowledge_id > 0
        archive_operation_id = operation_id_for_knowledge(runtime, knowledge_id)
        assert_r4_success_ledger(
            runtime,
            operation_id=archive_operation_id,
            knowledge_id=knowledge_id,
            expected_title=ARCHIVE_TITLE,
            expected_content=ARCHIVE_CONTENT,
        )

        plan_process = runtime.run_cli("embedding", "rebuild", "--force")
        assert plan_process.returncode == 0, plan_process.stderr
        plan_payload = _parse_json(plan_process.stdout)
        assert plan_payload["terminal"] == "success"
        assert plan_payload["operation"] == "embedding_rebuild"
        plan = plan_payload["plan"]
        assert type(plan) is dict
        assert plan["force"] is True
        plan_id = plan["plan_id"]
        assert type(plan_id) is str and len(plan_id) == 64
        prior_generation = plan["inspection"]["active_generation"]
        assert type(prior_generation) is str
        assert_no_public_leak(plan_process.stdout, runtime, ARCHIVE_CONTENT)

        rebuild = runtime.start_cli(
            "embedding",
            "rebuild",
            "--force",
            "--apply",
            "--confirm",
            plan_id,
            "--allow-network",
        )
        try:
            _release_step(harness, runtime, "embedding-rebuild-document")
            _release_step(harness, runtime, "embedding-rebuild-chunks")
            rebuild_stdout, rebuild_stderr = finish_process(rebuild)
        except BaseException:
            if rebuild.poll() is not None:
                early_stdout, early_stderr = rebuild.communicate(timeout=5)
                try:
                    early_payload = _parse_json(early_stderr or early_stdout)
                    early_code = early_payload.get("code")
                except (json.JSONDecodeError, AssertionError):
                    early_code = "unparseable"
                raise AssertionError(
                    f"public rebuild process ended before Provider barrier: code={early_code}"
                ) from None
            stop_process(rebuild)
            raise

        assert rebuild.returncode == 0
        rebuild_payload = _parse_json(rebuild_stdout)
        execution = rebuild_payload["execution"]
        assert rebuild_payload["terminal"] == "success"
        assert type(execution) is dict
        operation_id = execution["operation_id"]
        assert type(operation_id) is str and operation_id.startswith("embedding-rebuild-")
        assert execution["inspection"]["state"] == "ready"
        assert execution["inspection"]["active_generation"] == execution["generation_id"]
        assert execution["inspection"]["previous_generation"] == prior_generation
        assert execution["generation_id"] != prior_generation
        assert execution["audit_completion_pending"] is False
        assert execution["ledger_completion_pending"] is False
        assert type(execution["generation_id"]) is str
        assert type(execution["ledger"]) is dict
        assert execution["ledger"]["state"] == "completed"
        assert execution["ledger"]["reservation"]["state"] == "settled"
        assert "reservation_id" not in execution["ledger"]["reservation"]
        assert (
            execution["ledger"]["reservation"]["settled_tokens"]
            == execution["ledger"]["reservation"]["reserved_tokens"]
        )
        assert execution["ledger"]["provider_usage"]["embedding_input_tokens"] == 34
        assert_no_public_leak(
            rebuild_stdout,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
        )
        assert_no_public_leak(
            rebuild_stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        durable = EmbeddingRebuildLedger(runtime.layout).get_operation(operation_id)
        assert durable is not None
        assert durable.state is EmbeddingRebuildLedgerState.COMPLETED
        assert durable.reservation is not None
        assert durable.reservation.state == "settled"
        assert durable.provider_usage is not None
        assert durable.provider_usage.embedding_input_tokens == 34
        assert durable.reservation.settled_tokens == durable.reservation.reserved_tokens
        assert durable.generation_id == execution["generation_id"]

        # The public result proves activation; the isolated owned runtime
        # snapshot additionally proves retention rather than a hidden fallback
        # to a pre-R4 flat directory.
        pointer = RuntimeSnapshotStore(runtime.layout).read().payload["embedding_index"]
        assert pointer["active_generation"] == execution["generation_id"]
        assert pointer["previous_generation"] == prior_generation
        assert {prior_generation, execution["generation_id"]}.issubset(
            set(pointer["retained_generations"])
        )
        assert not any(
            (runtime.layout.vector_index_dir / name).exists()
            for name in (
                "doc_vectors.idx",
                "doc_vectors_metadata.json",
                "chunk_vectors.idx",
                "chunk_vectors_metadata.json",
            )
        )

        fresh_inspection = runtime.run_cli("embedding", "inspect")
        assert fresh_inspection.returncode == 0, fresh_inspection.stderr
        fresh_index = _parse_json(fresh_inspection.stdout)["inspection"]["index"]
        assert fresh_index["state"] == "ready"
        assert fresh_index["active_generation"] == execution["generation_id"]
        assert fresh_index["previous_generation"] == prior_generation

        show = runtime.run_cli("show", str(knowledge_id), "--raw")
        assert show.returncode == 0
        assert " ".join(show.stdout.split()) == ARCHIVE_CONTENT

        for strategy, step_id in (
            ("vector", "embedding-query-1"),
            ("hybrid", "embedding-query-2"),
        ):
            search = runtime.start_cli(
                "search",
                SEARCH_QUERY,
                "--strategy",
                strategy,
                "--format",
                "json",
            )
            try:
                _release_step(harness, runtime, step_id)
                search_stdout, search_stderr = finish_process(search)
            except BaseException:
                stop_process(search)
                raise
            assert search.returncode == 0
            search_payload = _parse_json(search_stdout)
            assert search_payload["status"] == "success"
            assert search_payload["strategy"] == strategy
            assert any(
                result["entry_id"] == knowledge_id
                for result in search_payload["results"]
            )
            assert_no_public_leak(search_stdout, runtime)
            assert_no_public_leak(
                search_stderr,
                runtime,
                ARCHIVE_TITLE,
                ARCHIVE_CONTENT,
                SEARCH_QUERY,
            )

        harness.assert_redacted_telemetry(
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )


@pytest.mark.slow
def test_public_cli_policy_gates_do_not_construct_a_manual_provider(
    tmp_path: Path,
) -> None:
    """The independent hard cap stops before a Provider/stage is created."""

    scenario_id = "cli-r4p1-rebuild-budget-paused"
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=archive_only_provider_script(scenario_id),
    ) as harness:
        runtime = R4BlackboxRuntime.create(
            tmp_path / "product",
            provider_base_url=harness.base_url,
        )
        archive = runtime.start_cli(
            "archive-text",
            ARCHIVE_CONTENT,
            "--title",
            ARCHIVE_TITLE,
            "--format",
            "json",
        )
        try:
            for step_id in (
                "chat-1",
                "chat-2",
                "embedding-archive-1",
                "embedding-archive-2",
            ):
                _release_step(harness, runtime, step_id)
            archive_stdout, _ = finish_process(archive)
        except BaseException:
            stop_process(archive)
            raise
        assert archive.returncode == 0
        knowledge_id = _parse_json(archive_stdout)["knowledge_id"]
        assert type(knowledge_id) is int

        config_payload = json.loads(runtime.user_config_path.read_text(encoding="utf-8"))
        manual_budget = config_payload["ai"]["embedding_rebuild"]["token_budget"]
        manual_budget["daily_total_tokens"] = None
        manual_budget["monthly_total_tokens"] = None
        runtime.user_config_path.write_text(
            json.dumps(config_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        planned = runtime.run_cli("embedding", "rebuild", "--force")
        assert planned.returncode == 0
        plan_id = _parse_json(planned.stdout)["plan"]["plan_id"]
        assert type(plan_id) is str
        authorization_blocked = runtime.run_cli(
            "embedding",
            "rebuild",
            "--force",
            "--apply",
            "--confirm",
            plan_id,
            "--allow-network",
        )

        assert authorization_blocked.returncode == 1
        authorization_payload = _parse_json(authorization_blocked.stderr)
        assert authorization_payload["code"] == "embedding_rebuild_authorization_required"
        assert_no_public_leak(
            authorization_blocked.stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
        )
        assert EmbeddingRebuildLedger(runtime.layout).read_operations() == ()

        manual_budget["daily_total_tokens"] = 0
        manual_budget["monthly_total_tokens"] = 1_000_000
        runtime.user_config_path.write_text(
            json.dumps(config_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        paused_plan = runtime.run_cli("embedding", "rebuild", "--force")
        assert paused_plan.returncode == 0
        paused_plan_id = _parse_json(paused_plan.stdout)["plan"]["plan_id"]
        assert type(paused_plan_id) is str
        budget_blocked = runtime.run_cli(
            "embedding",
            "rebuild",
            "--force",
            "--apply",
            "--confirm",
            paused_plan_id,
            "--allow-network",
        )

        assert budget_blocked.returncode == 1
        budget_payload = _parse_json(budget_blocked.stderr)
        assert budget_payload["code"] == "embedding_budget_paused"
        assert_no_public_leak(
            budget_blocked.stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
        )
        operations = EmbeddingRebuildLedger(runtime.layout).read_operations()
        assert len(operations) == 1
        assert operations[0].state is EmbeddingRebuildLedgerState.BUDGET_PAUSED
        assert operations[0].reservation is None
        harness.assert_redacted_telemetry(ARCHIVE_TITLE, ARCHIVE_CONTENT)


@pytest.mark.slow
def test_public_cli_rebuild_replay_is_fenced_before_a_second_provider_call(
    tmp_path: Path,
) -> None:
    """Two real CLI processes cannot pay twice for one material transition."""

    scenario_id = "cli-r4p1-public-rebuild-replay"
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=manual_embedding_rebuild_provider_script(scenario_id),
    ) as harness:
        runtime = R4BlackboxRuntime.create(
            tmp_path / "product",
            provider_base_url=harness.base_url,
        )
        archive = runtime.start_cli(
            "archive-text",
            ARCHIVE_CONTENT,
            "--title",
            ARCHIVE_TITLE,
            "--format",
            "json",
        )
        try:
            for step_id in (
                "chat-1",
                "chat-2",
                "embedding-archive-1",
                "embedding-archive-2",
            ):
                _release_step(harness, runtime, step_id)
            archive_stdout, _ = finish_process(archive)
        except BaseException:
            stop_process(archive)
            raise
        knowledge_id = _parse_json(archive_stdout)["knowledge_id"]
        assert type(knowledge_id) is int

        planned = runtime.run_cli("embedding", "rebuild", "--force")
        assert planned.returncode == 0, planned.stderr
        plan_id = _parse_json(planned.stdout)["plan"]["plan_id"]
        assert type(plan_id) is str

        first = runtime.start_cli(
            "embedding",
            "rebuild",
            "--force",
            "--apply",
            "--confirm",
            plan_id,
            "--allow-network",
        )
        try:
            harness.await_request("embedding-rebuild-document")
            runtime.prove_writer_lease_is_free()
            second = runtime.start_cli(
                "embedding",
                "rebuild",
                "--force",
                "--apply",
                "--confirm",
                plan_id,
                "--allow-network",
            )
            second_stdout, second_stderr = finish_process(second)
            assert second.returncode == 1
            second_payload = _parse_json(second_stderr or second_stdout)
            assert second_payload["code"] == "write_busy"
            assert_no_public_leak(
                second_stderr or second_stdout,
                runtime,
                ARCHIVE_TITLE,
                ARCHIVE_CONTENT,
            )

            harness.continue_request("embedding-rebuild-document")
            harness.await_event(
                "step_completed",
                step_id="embedding-rebuild-document",
            )
            _release_step(harness, runtime, "embedding-rebuild-chunks")
            first_stdout, first_stderr = finish_process(first)
        except BaseException:
            stop_process(first)
            raise

        assert first.returncode == 0, first_stderr
        assert _parse_json(first_stdout)["execution"]["ledger"]["state"] == "completed"

        for strategy, step_id in (
            ("vector", "embedding-query-1"),
            ("hybrid", "embedding-query-2"),
        ):
            search = runtime.start_cli(
                "search",
                SEARCH_QUERY,
                "--strategy",
                strategy,
                "--format",
                "json",
            )
            try:
                _release_step(harness, runtime, step_id)
                search_stdout, _ = finish_process(search)
            except BaseException:
                stop_process(search)
                raise
            payload = _parse_json(search_stdout)
            assert any(result["entry_id"] == knowledge_id for result in payload["results"])

        operations = EmbeddingRebuildLedger(runtime.layout).read_operations()
        assert len(operations) == 1
        assert operations[0].state is EmbeddingRebuildLedgerState.COMPLETED
        harness.assert_redacted_telemetry(ARCHIVE_TITLE, ARCHIVE_CONTENT, SEARCH_QUERY)


@pytest.mark.slow
def test_public_cli_rebuild_confirmation_is_root_bound_and_process_stable(
    tmp_path: Path,
) -> None:
    """ER-01/02: another root rejects A's plan; a fresh A process can execute it."""

    scenario_id = "cli-w2-rebuild-root-identity"
    script = manual_embedding_rebuild_provider_script(scenario_id)
    script["steps"] = script["steps"][4:6]
    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id=scenario_id,
        script=script,
    ) as harness:
        first = R4BlackboxRuntime.create(
            tmp_path / "first-root", provider_base_url=harness.base_url
        )
        second = R4BlackboxRuntime.create(
            tmp_path / "second-root", provider_base_url=harness.base_url
        )
        assert first.config.data_root_identity != second.config.data_root_identity
        for runtime in (first, second):
            # Owned source fixture only: the operations being accepted below
            # still use actual public CLI subprocesses and the real Provider API.
            with write_lease_scope(runtime.layout):
                markdown = MarkdownStore(runtime.layout.vault_dir)
                entry = Entry(
                    title=ARCHIVE_TITLE,
                    source_type="text",
                    content=ARCHIVE_CONTENT,
                )
                write_plan = markdown.plan_save(entry)
                markdown.save_planned(write_plan, entry)
                SQLiteStore(runtime.layout.db_path).insert_entry_with_chunks(
                    entry, write_plan.relative_path, [ARCHIVE_CONTENT]
                )

        first_planned = first.run_cli("embedding", "rebuild")
        assert first_planned.returncode == 0, first_planned.stderr
        first_plan = _parse_json(first_planned.stdout)["plan"]
        plan_id = first_plan["plan_id"]
        assert type(plan_id) is str and len(plan_id) == 64

        fresh_first_planned = first.run_cli("embedding", "rebuild")
        assert fresh_first_planned.returncode == 0, fresh_first_planned.stderr
        assert _parse_json(fresh_first_planned.stdout)["plan"]["plan_id"] == plan_id

        second_planned = second.run_cli("embedding", "rebuild")
        assert second_planned.returncode == 0, second_planned.stderr
        second_plan = _parse_json(second_planned.stdout)["plan"]
        for field in ("contract", "source", "active_generation", "active_manifest_sha256"):
            assert second_plan["inspection"][field] == first_plan["inspection"][field]
        assert second_plan["plan_id"] != plan_id
        second_snapshot = RuntimeSnapshotStore(second.layout).read()

        wrong_root = second.run_cli(
            "embedding", "rebuild", "--apply", "--confirm", plan_id, "--allow-network"
        )
        assert wrong_root.returncode == 1
        assert _parse_json(wrong_root.stderr)["code"] == "runtime_plan_stale"
        assert RuntimeSnapshotStore(second.layout).read() == second_snapshot
        assert EmbeddingRebuildLedger(second.layout).read_operations() == ()
        assert not (second.layout.vector_index_dir / "staging").exists()
        assert not (second.layout.vector_index_dir / "generations").exists()

        rebuild = first.start_cli(
            "embedding", "rebuild", "--apply", "--confirm", plan_id, "--allow-network"
        )
        try:
            _release_step(harness, first, "embedding-rebuild-document")
            _release_step(harness, first, "embedding-rebuild-chunks")
            stdout, stderr = finish_process(rebuild)
        except BaseException:
            stop_process(rebuild)
            raise
        assert rebuild.returncode == 0, stderr
        result = _parse_json(stdout)
        assert result["execution"]["inspection"]["state"] == "ready"
        assert result["execution"]["ledger"]["state"] == "completed"
        assert len(EmbeddingRebuildLedger(first.layout).read_operations()) == 1
        for payload, runtime in (
            (first_planned.stdout, first),
            (fresh_first_planned.stdout, first),
            (second_planned.stdout, second),
            (wrong_root.stderr, second),
            (stdout, first),
            (stderr, first),
        ):
            assert_no_public_leak(payload, runtime, ARCHIVE_TITLE, ARCHIVE_CONTENT)
        # Exactly the two successful A requests prove B's rejection made none.
        harness.assert_redacted_telemetry(ARCHIVE_TITLE, ARCHIVE_CONTENT)
