"""R4 foundation contracts for the separate local audit trace channel."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.runtime.audit import AUDIT_SCHEMA_VERSION, REDACTED_VALUE, AuditTrace, AuditTraceError
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout


def _layout(tmp_path: Path) -> RuntimeLayout:
    resources_root = tmp_path / "resources"
    resources_root.mkdir()
    return RuntimeLayout.resolve(
        resources_root=resources_root,
        user_data_root=tmp_path / "data",
        environment={},
    )


def test_audit_trace_keeps_full_article_prompt_but_redacts_nested_credentials_and_urls(
    tmp_path: Path,
) -> None:
    api_key = "sk-r4-configured-provider-key"
    authorization = "Bearer r4-authorization-secret"
    cookie = "session=r4-cookie-secret"
    query_secret = "r4-url-query-secret"
    article = "完整文章正文：可复现的互联网信源摘录。"
    prompt = "完整 Prompt：请提炼文章的时间线与来源。"
    trace = AuditTrace(
        _layout(tmp_path),
        secret_values=(api_key,),
        now=lambda: datetime(2026, 8, 20, 1, 2, 3, tzinfo=timezone.utc),
    )

    path = trace.append(
        {
            "operation": "archive_text",
            "article": article,
            "prompt": prompt,
            "reproduction_note": f"do not retain {api_key}",
            "provider": {
                "api_key": api_key,
                "headers": {
                    "Authorization": authorization,
                    "Cookie": cookie,
                },
            },
            "nested": [
                {"client_secret": "r4-nested-client-secret"},
                {"token": "r4-nested-token"},
            ],
            "source_url": (
                "https://user:r4-url-password@example.test/v1"
                f"?api_key={query_secret}&region=cn"
                "#access_token=r4-fragment-secret"
            ),
            "quoted_url": f"来源 https://example.test/x?token={query_secret}&page=2。",
            "inline": (
                "Authorization: Bearer r4-inline-secret; "
                "cookie=r4-inline-cookie; api key = r4-inline-api-key"
            ),
        }
    )

    assert path == trace.path
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    raw = lines[0]
    for secret in (
        api_key,
        authorization,
        cookie,
        query_secret,
        "r4-url-password",
        "r4-fragment-secret",
        "r4-nested-client-secret",
        "r4-nested-token",
        "r4-inline-secret",
        "r4-inline-cookie",
        "r4-inline-api-key",
    ):
        assert secret not in raw

    record = json.loads(raw)
    assert record["schema_version"] == AUDIT_SCHEMA_VERSION
    assert record["recorded_at"] == "2026-08-20T01:02:03Z"
    event = record["event"]
    assert event["article"] == article
    assert event["prompt"] == prompt
    assert event["reproduction_note"] == f"do not retain {REDACTED_VALUE}"
    assert event["provider"]["api_key"] == REDACTED_VALUE
    assert event["provider"]["headers"]["Authorization"] == REDACTED_VALUE
    assert event["provider"]["headers"]["Cookie"] == REDACTED_VALUE
    assert event["nested"] == [
        {"client_secret": REDACTED_VALUE},
        {"token": REDACTED_VALUE},
    ]
    assert "user:" not in event["source_url"]
    assert "api_key=" in event["source_url"]
    assert "region=cn" in event["source_url"]
    assert "access_token=" in event["source_url"]
    assert "page=2" in event["quoted_url"]
    assert REDACTED_VALUE in event["inline"]


def test_audit_trace_is_jsonl_and_uses_only_layout_log_root(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    trace = AuditTrace(
        layout,
        secret_values=(),
        now=lambda: datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    trace.append({"operation": "first", "article": "first full article"})
    trace.append({"operation": "second", "prompt": "second full prompt"})

    assert trace.path == layout.log_dir / "audit.jsonl"
    assert trace.path.is_file()
    assert not list(layout.user_data_root.glob("audit.jsonl"))
    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"]["operation"] for record in records] == ["first", "second"]


def test_audit_trace_rejects_unsupported_payload_without_secret_reflection(
    tmp_path: Path,
) -> None:
    api_key = "sk-r4-object-secret"

    class UnsafeValue:
        def __repr__(self) -> str:
            return api_key

    trace = AuditTrace(_layout(tmp_path), secret_values=(api_key,))

    with pytest.raises(AuditTraceError) as captured:
        trace.append({"article": UnsafeValue()})

    assert str(captured.value) == "审计追踪无法安全写入"
    assert api_key not in str(captured.value)
    assert not trace.path.exists()


def test_audit_operation_captures_full_context_and_typed_failure_without_message(
    tmp_path: Path,
) -> None:
    api_key = "sk-r4-operation-key"
    secret_message = f"Provider rejected {api_key}"
    article = "完整文章正文：归档前的内容必须完整保留。"
    prompt = "完整 Prompt：请列出可引用的事实。"
    trace = AuditTrace(
        _layout(tmp_path),
        secret_values=(api_key,),
        now=lambda: datetime(2026, 8, 20, 4, 5, 6, tzinfo=timezone.utc),
    )
    captured_context = {
        "article": article,
        "prompt": prompt,
        "configuration_generation": 7,
        "data_root_identity": "sha256:synthetic-root",
        "provider": {"api_key": api_key},
    }

    with trace.operation("embedding_rebuild", context=captured_context) as operation:
        # The timeline has already captured its own safe snapshot; a later
        # caller mutation must not turn the terminal record into Config B.
        captured_context["configuration_generation"] = 8
        captured_context["article"] = "不应出现在结束记录的替换文章"
        operation.fail_runtime_error(
            PKVRuntimeError(
                ErrorCode.WRITE_BUSY,
                secret_message,
                stage="write_lease",
                recoverable=True,
            ),
            details={"plan_id": "r4-plan-1", "authorization": api_key},
        )

    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"]["phase"] for record in records] == ["started", "failed"]
    for record in records:
        event = record["event"]
        assert event["operation"] == "embedding_rebuild"
        assert event["context"]["article"] == article
        assert event["context"]["prompt"] == prompt
        assert event["context"]["configuration_generation"] == 7
        assert event["context"]["provider"]["api_key"] == REDACTED_VALUE
    failed = records[-1]["event"]
    assert failed["failure"] == {
        "code": "write_busy",
        "stage": "write_lease",
        "recoverable": True,
    }
    assert failed["details"] == {"plan_id": "r4-plan-1", "authorization": REDACTED_VALUE}
    raw = trace.path.read_text(encoding="utf-8")
    assert secret_message not in raw
    assert api_key not in raw


def test_audit_operation_unknown_exception_never_reads_exception_text_or_creates_on_inspect(
    tmp_path: Path,
) -> None:
    api_key = "sk-r4-exception-key"
    trace = AuditTrace(_layout(tmp_path), secret_values=(api_key,))

    # A read-only inspection that merely creates the trace object remains pure.
    assert not trace.path.exists()

    class UnsafeFailure(RuntimeError):
        def __str__(self) -> str:
            return api_key

    with pytest.raises(UnsafeFailure):
        with trace.operation("archive_text", context={"article": "全文"}):
            raise UnsafeFailure()

    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"]["phase"] for record in records] == ["started", "failed"]
    failure = records[-1]["event"]["failure"]
    assert failure == {"code": "operation_failed", "recoverable": False}
    assert api_key not in trace.path.read_text(encoding="utf-8")


def test_audit_operation_requires_stable_identifiers(tmp_path: Path) -> None:
    trace = AuditTrace(_layout(tmp_path), secret_values=())

    with pytest.raises(AuditTraceError):
        with trace.operation("archive text", context={}):
            pass

    assert not trace.path.exists()


def test_audit_operation_preserves_business_error_when_failure_trace_cannot_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = AuditTrace(_layout(tmp_path), secret_values=())
    original_append = trace.append

    def reject_terminal(event: dict[str, object]) -> Path:
        if event.get("phase") == "failed":
            raise AuditTraceError()
        return original_append(event)

    monkeypatch.setattr(trace, "append", reject_terminal)

    class BusinessFailure(RuntimeError):
        pass

    with pytest.raises(BusinessFailure):
        with trace.operation("archive_text", context={"article": "全文"}):
            raise BusinessFailure()

    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"]["phase"] for record in records] == ["started"]


def test_audit_operation_auto_completes_when_caller_has_no_result(tmp_path: Path) -> None:
    trace = AuditTrace(_layout(tmp_path), secret_values=())

    with trace.operation("delete_entry", context={"knowledge_id": 42}) as operation:
        assert not operation.terminal

    records = [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"]["phase"] for record in records] == ["started", "completed"]
    assert records[-1]["event"]["context"] == {"knowledge_id": 42}
