"""True CLI process-boundary R4 source acceptance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.blackbox.r4_fullflow_support import (
    ProviderHarness,
    R4BlackboxRuntime,
    assert_no_public_leak,
    assert_r4_success_ledger,
    finish_process,
    operation_id_for_knowledge,
    stop_process,
)


ARCHIVE_TITLE = "R4 CLI deterministic semantic archive"
ARCHIVE_CONTENT = (
    "Orchid relays preserve durable semantic memory across a complete process "
    "restart. This sentence exists only inside the isolated R4 black-box root."
)
SEARCH_QUERY = "durable semantic memory after restart"


def _release_provider_step(
    harness: ProviderHarness,
    runtime: R4BlackboxRuntime,
    step_id: str,
) -> None:
    event = harness.await_request(step_id)
    assert event["request_number"] >= 1
    assert event["item_count"] == 1
    assert isinstance(event["request_sha256"], str)
    assert len(event["request_sha256"]) == 64
    runtime.prove_writer_lease_is_free()
    harness.continue_request(step_id)
    harness.await_event("step_completed", step_id=step_id)


def _parse_cli_json(stdout: str) -> dict[str, object]:
    payload = json.loads(stdout)
    assert type(payload) is dict
    return payload


@pytest.mark.slow
def test_cli_archive_text_restart_vector_and_hybrid_fullflow(tmp_path: Path) -> None:
    """Archive through one CLI and retrieve through three fresh CLI processes."""

    with ProviderHarness.start(
        tmp_path / "provider",
        scenario_id="cli-r4-success",
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
                _release_provider_step(harness, runtime, step_id)
            archive_stdout, archive_stderr = finish_process(archive)
        except BaseException:
            stop_process(archive)
            raise

        assert archive.returncode == 0
        archive_payload = _parse_cli_json(archive_stdout)
        assert archive_payload == {
            "file_path": archive_payload["file_path"],
            "issues": [],
            "knowledge_id": archive_payload["knowledge_id"],
            "status": "ready",
            "tags": ["r4", "blackbox", "semantic"],
            "terminal": "success",
            "title": ARCHIVE_TITLE,
        }
        assert isinstance(archive_payload["file_path"], str)
        assert archive_payload["file_path"]
        assert type(archive_payload["knowledge_id"]) is int
        knowledge_id = archive_payload["knowledge_id"]
        assert knowledge_id > 0
        assert_no_public_leak(archive_stdout, runtime, ARCHIVE_CONTENT)
        assert_no_public_leak(
            archive_stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        operation_id = operation_id_for_knowledge(runtime, knowledge_id)
        durable = assert_r4_success_ledger(
            runtime,
            operation_id=operation_id,
            knowledge_id=knowledge_id,
            expected_title=ARCHIVE_TITLE,
            expected_content=ARCHIVE_CONTENT,
        )

        show = runtime.run_cli("show", str(knowledge_id), "--raw")
        assert show.returncode == 0
        assert " ".join(show.stdout.split()) == ARCHIVE_CONTENT
        assert_no_public_leak(show.stdout, runtime)
        assert_no_public_leak(
            show.stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        vector = runtime.start_cli(
            "search",
            SEARCH_QUERY,
            "--strategy",
            "vector",
            "--format",
            "json",
        )
        try:
            _release_provider_step(harness, runtime, "embedding-query-1")
            vector_stdout, vector_stderr = finish_process(vector)
        except BaseException:
            stop_process(vector)
            raise
        assert vector.returncode == 0
        vector_payload = _parse_cli_json(vector_stdout)
        assert vector_payload["status"] == "success"
        assert vector_payload["strategy"] == "vector"
        assert vector_payload["issues"] == []
        assert any(
            result["entry_id"] == knowledge_id for result in vector_payload["results"]
        )
        assert_no_public_leak(vector_stdout, runtime)
        assert_no_public_leak(
            vector_stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        hybrid = runtime.start_cli(
            "search",
            SEARCH_QUERY,
            "--strategy",
            "hybrid",
            "--format",
            "json",
        )
        try:
            _release_provider_step(harness, runtime, "embedding-query-2")
            hybrid_stdout, hybrid_stderr = finish_process(hybrid)
        except BaseException:
            stop_process(hybrid)
            raise
        assert hybrid.returncode == 0
        hybrid_payload = _parse_cli_json(hybrid_stdout)
        assert hybrid_payload["status"] == "success"
        assert hybrid_payload["strategy"] == "hybrid"
        assert hybrid_payload["issues"] == []
        assert any(
            result["entry_id"] == knowledge_id for result in hybrid_payload["results"]
        )
        assert_no_public_leak(hybrid_stdout, runtime)
        assert_no_public_leak(
            hybrid_stderr,
            runtime,
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )

        assert operation_id_for_knowledge(runtime, knowledge_id) == operation_id
        assert_r4_success_ledger(
            runtime,
            operation_id=operation_id,
            knowledge_id=knowledge_id,
            expected_title=ARCHIVE_TITLE,
            expected_content=ARCHIVE_CONTENT,
        ) == durable
        harness.assert_redacted_telemetry(
            ARCHIVE_TITLE,
            ARCHIVE_CONTENT,
            SEARCH_QUERY,
        )
