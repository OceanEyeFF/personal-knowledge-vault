"""W4 adapter contracts at the public SDK seam; no Provider or storage effects."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from pkv_kernel import embedding as sdk
from src import main as cli_main
from src.cli import commands
from src.mcp import server, tools
from src.runtime.errors import ErrorCode, PKVRuntimeError


PLAN_ID = "a" * 64
OTHER_PLAN_ID = "b" * 64
OPERATION_ID = "embedding-rebuild-" + "c" * 32
WARNING = {
    "code": ErrorCode.AUDIT_COMPLETION_PENDING.value,
    "message": "已提交，完成记录仍待对齐。",
    "stage": "embedding_publication",
    "recoverable": True,
}
READ_TOOLS = {
    "inspect_embedding_rebuild_v2",
    "plan_embedding_rebuild_v2",
    "inspect_embedding_operation_v2",
    "plan_embedding_publication_v2",
}
WRITE_TOOLS = {"execute_embedding_rebuild_v2", "execute_embedding_publication_v2"}


class _PublicHandle:
    """A display-only stand-in; adapters must not depend on private handle state."""

    def __init__(self, **payload: object) -> None:
        self._payload = deepcopy(payload)

    @property
    def plan_id(self) -> str:
        return self._payload["plan_id"]

    def to_dict(self) -> dict[str, object]:
        return deepcopy(self._payload)


@pytest.fixture
def public_seam(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    config = object()
    handles = {
        "inspection": _PublicHandle(schema="pkv.embedding.inspection.v2", index={}),
        "plan": _PublicHandle(schema="pkv.embedding.plan.v2", plan_id=PLAN_ID),
        "operation": _PublicHandle(
            schema="pkv.embedding.operation.v2", operation_id=OPERATION_ID,
            effective_state="awaiting_publish", issues=[],
        ),
        "publication_plan": _PublicHandle(
            schema="pkv.embedding.publication-plan.v2", plan_id=PLAN_ID,
            operation_id=OPERATION_ID, action="publish_only", requires_network=False,
        ),
        "confirmation": _PublicHandle(schema="pkv.embedding.confirmation.v2", plan_id=PLAN_ID),
        "publication_confirmation": _PublicHandle(
            schema="pkv.embedding.publication-confirmation.v2", plan_id=PLAN_ID,
        ),
        "execution": _PublicHandle(
            schema="pkv.embedding.execution.v2", operation_id=OPERATION_ID,
            status="ready", publication_committed=True, provider_called=True,
            ledger_completion_pending=False, audit_completion_pending=True, issues=[WARNING],
        ),
    }
    methods = {
        "inspect_embedding_rebuild_v2": Mock(return_value=handles["inspection"]),
        "plan_embedding_rebuild_v2": Mock(return_value=handles["plan"]),
        "confirm_embedding_rebuild_v2": Mock(return_value=handles["confirmation"]),
        "execute_embedding_rebuild_v2": Mock(return_value=handles["execution"]),
        "inspect_embedding_operation_v2": Mock(return_value=handles["operation"]),
        "plan_embedding_publication_v2": Mock(return_value=handles["publication_plan"]),
        "confirm_embedding_publication_v2": Mock(return_value=handles["publication_confirmation"]),
        "execute_embedding_publication_v2": Mock(return_value=handles["execution"]),
    }
    for name, method in methods.items():
        monkeypatch.setattr(sdk, name, method, raising=False)
    cli_config = Mock(return_value=config)
    mcp_config = Mock(return_value=config)
    monkeypatch.setattr(commands, "_load_config", cli_config)
    monkeypatch.setattr(tools, "get_runtime_config_for_lifecycle", mcp_config)
    return SimpleNamespace(config=config, handles=handles, methods=methods, cli_config=cli_config, mcp_config=mcp_config)


def _envelope(payload: dict[str, object], operation: str, status: str) -> None:
    assert set(payload) == {"schema", "status", "terminal", "operation", "result", "issues"}
    assert payload["schema"] == "pkv.lifecycle-response.v2"
    assert payload["status"] == payload["terminal"] == status
    assert payload["operation"] == operation
    if status == "error":
        assert payload["result"] is None
        assert payload["issues"]


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (("embedding", "--help"), 0),
        (("embedding", "inspect-v2", "--help"), 0),
        (("embedding", "rebuild-v2", "--help"), 0),
        (("embedding", "operation-v2", "--help"), 0),
        (("embedding", "publish-v2", "--help"), 0),
        (("embedding", "operation-v2"), 2),
        (("embedding", "publish-v2"), 2),
        (("embedding", "operation-v2", "../../private"), 2),
        (("embedding", "operation-v2", "embedding-rebuild-" + "C" * 32), 2),
        (("embedding", "rebuild-v2", "--confirm", "A" * 64), 2),
        (("embedding", "publish-v2", OPERATION_ID, "--confirm", "bad"), 2),
        (("embedding", "publish-v2", OPERATION_ID, "--allow-network"), 2),
        (("embedding", "publish-v2", OPERATION_ID, "--force"), 2),
        (("embedding", "inspect-v2", "--data-root", "private"), 2),
        (("embedding", "rebuild-v2", "--unknown"), 2),
    ],
)
def test_cli_v2_parsing_precedes_runtime(
    monkeypatch: pytest.MonkeyPatch, public_seam: SimpleNamespace,
    arguments: tuple[str, ...], exit_code: int,
) -> None:
    business_gate = Mock(side_effect=AssertionError("runtime gate before parsing"))
    monkeypatch.setattr(cli_main, "_prepare_cli_runtime", business_gate)
    public_seam.cli_config.side_effect = AssertionError("configuration before parsing")
    result = CliRunner().invoke(cli_main.cli, arguments)
    assert result.exit_code == exit_code, result.output
    business_gate.assert_not_called()
    public_seam.cli_config.assert_not_called()
    for method in public_seam.methods.values():
        method.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ("rebuild-v2", "--apply"),
        ("rebuild-v2", "--confirm", PLAN_ID),
        ("rebuild-v2", "--allow-network"),
        ("publish-v2", OPERATION_ID, "--apply"),
        ("publish-v2", OPERATION_ID, "--confirm", PLAN_ID),
    ],
)
def test_cli_v2_incomplete_authority_is_rejected_before_configuration(
    public_seam: SimpleNamespace, arguments: tuple[str, ...],
) -> None:
    result = CliRunner().invoke(cli_main.cli, ("embedding", *arguments))
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["issues"][0]["code"] == "confirmation_required"
    public_seam.cli_config.assert_not_called()
    for method in public_seam.methods.values():
        method.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "operation", "handle"),
    [
        (("inspect-v2",), "inspect_embedding_rebuild_v2", "inspection"),
        (("rebuild-v2",), "plan_embedding_rebuild_v2", "plan"),
        (("operation-v2", OPERATION_ID), "inspect_embedding_operation_v2", "operation"),
        (("publish-v2", OPERATION_ID), "plan_embedding_publication_v2", "publication_plan"),
    ],
)
def test_cli_v2_read_operations_use_fixed_envelope(
    public_seam: SimpleNamespace, arguments: tuple[str, ...], operation: str, handle: str,
) -> None:
    result = CliRunner().invoke(cli_main.cli, ("embedding", *arguments))
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    _envelope(payload, operation, "success")
    assert payload["result"] == public_seam.handles[handle].to_dict()
    public_seam.cli_config.assert_called_once_with()
    public_seam.methods["execute_embedding_rebuild_v2"].assert_not_called()
    public_seam.methods["execute_embedding_publication_v2"].assert_not_called()


@pytest.mark.parametrize("publication", (False, True))
def test_cli_v2_reconstructs_current_plan_before_confirmation(
    public_seam: SimpleNamespace, publication: bool,
) -> None:
    arguments = ["embedding", "publish-v2", OPERATION_ID] if publication else ["embedding", "rebuild-v2", "--force"]
    arguments += ["--apply", "--confirm", PLAN_ID]
    if not publication:
        arguments.append("--allow-network")
    result = CliRunner().invoke(cli_main.cli, arguments)
    assert result.exit_code == 0, result.output
    prefix = "publication" if publication else "rebuild"
    operation = f"execute_embedding_{prefix}_v2"
    payload = json.loads(result.stdout)
    _envelope(payload, operation, "success")
    assert payload["issues"] == [WARNING]
    plan = public_seam.handles["publication_plan" if publication else "plan"]
    confirmation = public_seam.handles["publication_confirmation" if publication else "confirmation"]
    public_seam.methods[operation].assert_called_once_with(plan, confirmation)
    expected = {"approved": True} if publication else {"approved": True, "allow_network": True}
    public_seam.methods[f"confirm_embedding_{prefix}_v2"].assert_called_once_with(plan, **expected)
    if publication:
        public_seam.methods["inspect_embedding_operation_v2"].assert_called_once_with(OPERATION_ID, config=public_seam.config)
        public_seam.methods["plan_embedding_publication_v2"].assert_called_once_with(public_seam.handles["operation"])
        public_seam.methods["inspect_embedding_rebuild_v2"].assert_not_called()
    else:
        public_seam.methods["inspect_embedding_rebuild_v2"].assert_called_once_with(public_seam.config)
        public_seam.methods["plan_embedding_rebuild_v2"].assert_called_once_with(public_seam.handles["inspection"], force=True)


@pytest.mark.parametrize("publication", (False, True))
def test_cli_v2_stale_plan_does_not_create_confirmation(public_seam: SimpleNamespace, publication: bool) -> None:
    arguments = ["embedding", "publish-v2", OPERATION_ID] if publication else ["embedding", "rebuild-v2"]
    result = CliRunner().invoke(cli_main.cli, [*arguments, "--apply", "--confirm", OTHER_PLAN_ID])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    prefix = "publication" if publication else "rebuild"
    _envelope(payload, f"execute_embedding_{prefix}_v2", "error")
    assert payload["issues"][0]["code"] == "runtime_plan_stale"
    public_seam.methods[f"confirm_embedding_{prefix}_v2"].assert_not_called()
    public_seam.methods[f"execute_embedding_{prefix}_v2"].assert_not_called()


def test_cli_v2_missing_network_permission_is_not_invented(public_seam: SimpleNamespace) -> None:
    public_seam.methods["confirm_embedding_rebuild_v2"].side_effect = PKVRuntimeError(
        ErrorCode.EMBEDDING_REBUILD_AUTHORIZATION_REQUIRED,
        "network permission required", stage="embedding_plan", recoverable=True,
    )
    result = CliRunner().invoke(cli_main.cli, ["embedding", "rebuild-v2", "--apply", "--confirm", PLAN_ID])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["issues"][0]["code"] == "embedding_rebuild_authorization_required"
    public_seam.methods["confirm_embedding_rebuild_v2"].assert_called_once_with(
        public_seam.handles["plan"], approved=True, allow_network=False,
    )
    public_seam.methods["execute_embedding_rebuild_v2"].assert_not_called()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("inspect_embedding_rebuild_v2", {"config": "/private"}),
        ("plan_embedding_rebuild_v2", {"force": "false"}),
        ("plan_embedding_rebuild_v2", {"force": 1}),
        ("plan_embedding_rebuild_v2", {"force": None}),
        ("execute_embedding_rebuild_v2", {}),
        ("execute_embedding_rebuild_v2", {"plan_id": "A" * 64}),
        ("execute_embedding_rebuild_v2", {"plan_id": PLAN_ID, "approved": "true"}),
        ("execute_embedding_rebuild_v2", {"plan_id": PLAN_ID, "allow_network": 1}),
        ("execute_embedding_rebuild_v2", {"plan_id": PLAN_ID, "force": 0}),
        ("execute_embedding_rebuild_v2", {"plan_id": PLAN_ID, "root": "/private"}),
        ("inspect_embedding_operation_v2", {}),
        ("inspect_embedding_operation_v2", {"operation_id": 1}),
        ("inspect_embedding_operation_v2", {"operation_id": "../../private"}),
        ("inspect_embedding_operation_v2", {"operation_id": "embedding-rebuild-" + "C" * 32}),
        ("plan_embedding_publication_v2", {"operation_id": OPERATION_ID, "force": False}),
        ("plan_embedding_publication_v2", {"operation_id": OPERATION_ID, "allow_network": False}),
        ("execute_embedding_publication_v2", {"operation_id": OPERATION_ID}),
        ("execute_embedding_publication_v2", {"plan_id": PLAN_ID}),
        ("execute_embedding_publication_v2", {"operation_id": OPERATION_ID, "plan_id": PLAN_ID, "approved": 1}),
        ("execute_embedding_publication_v2", {"operation_id": OPERATION_ID, "plan_id": PLAN_ID, "allow_network": False}),
        ("execute_embedding_publication_v2", {"operation_id": OPERATION_ID, "plan_id": PLAN_ID, "force": False}),
    ],
)
@pytest.mark.asyncio
async def test_mcp_v2_rejects_raw_coercion_and_extra_fields_before_config(
    public_seam: SimpleNamespace, name: str, arguments: dict[str, object],
) -> None:
    assert server.valid_embedding_v2_arguments(name, arguments) is False
    payload = await server.mcp.call_tool(name, arguments)
    _envelope(payload, name, "error")
    public_seam.mcp_config.assert_not_called()
    for method in public_seam.methods.values():
        method.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_v2_discovery_is_closed_and_preserves_readonly_hints() -> None:
    discovered = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert (READ_TOOLS | WRITE_TOOLS) <= set(discovered)
    for name in READ_TOOLS | WRITE_TOOLS:
        tool = discovered[name]
        assert tool.annotations.readOnlyHint is (name in READ_TOOLS)
        assert tool.inputSchema["additionalProperties"] is False
        assert not {"config", "root", "data_root"} & set(tool.inputSchema["properties"])
    publication_fields = discovered["execute_embedding_publication_v2"].inputSchema["properties"]
    assert set(publication_fields) == {"operation_id", "plan_id", "approved"}
    assert publication_fields["approved"]["default"] is False
    rebuild_fields = discovered["execute_embedding_rebuild_v2"].inputSchema["properties"]
    assert all(rebuild_fields[field]["default"] is False for field in ("approved", "allow_network", "force"))


@pytest.mark.parametrize(
    ("name", "arguments", "handle"),
    [
        ("inspect_embedding_rebuild_v2", {}, "inspection"),
        ("plan_embedding_rebuild_v2", {}, "plan"),
        ("inspect_embedding_operation_v2", {"operation_id": OPERATION_ID}, "operation"),
        ("plan_embedding_publication_v2", {"operation_id": OPERATION_ID}, "publication_plan"),
    ],
)
@pytest.mark.asyncio
async def test_mcp_v2_read_operations_reinspect_without_mutation(
    public_seam: SimpleNamespace, name: str, arguments: dict[str, object], handle: str,
) -> None:
    payload = await server.mcp.call_tool(name, arguments)
    _envelope(payload, name, "success")
    assert payload["result"] == public_seam.handles[handle].to_dict()
    public_seam.mcp_config.assert_called_once_with(require_ready=False)
    public_seam.methods["execute_embedding_rebuild_v2"].assert_not_called()
    public_seam.methods["execute_embedding_publication_v2"].assert_not_called()


@pytest.mark.parametrize("publication", (False, True))
@pytest.mark.asyncio
async def test_mcp_v2_exact_plan_confirmation_preserves_explicit_authority(
    public_seam: SimpleNamespace, publication: bool,
) -> None:
    prefix = "publication" if publication else "rebuild"
    name = f"execute_embedding_{prefix}_v2"
    arguments = {"plan_id": PLAN_ID, "approved": True}
    if publication:
        arguments["operation_id"] = OPERATION_ID
    else:
        arguments.update(allow_network=True, force=True)
    payload = await server.mcp.call_tool(name, arguments)
    _envelope(payload, name, "success")
    assert payload["issues"] == [WARNING]
    plan = public_seam.handles["publication_plan" if publication else "plan"]
    expected = {"approved": True} if publication else {"approved": True, "allow_network": True}
    public_seam.methods[f"confirm_embedding_{prefix}_v2"].assert_called_once_with(plan, **expected)
    public_seam.methods[name].assert_called_once()
    if publication:
        public_seam.methods["inspect_embedding_operation_v2"].assert_called_once_with(OPERATION_ID, config=public_seam.config)
        public_seam.methods["inspect_embedding_rebuild_v2"].assert_not_called()
    else:
        public_seam.methods["plan_embedding_rebuild_v2"].assert_called_once_with(public_seam.handles["inspection"], force=True)


@pytest.mark.parametrize("publication", (False, True))
@pytest.mark.asyncio
async def test_mcp_v2_stale_confirmation_never_reaches_confirm(public_seam: SimpleNamespace, publication: bool) -> None:
    prefix = "publication" if publication else "rebuild"
    name = f"execute_embedding_{prefix}_v2"
    arguments = {"plan_id": OTHER_PLAN_ID, "approved": True}
    if publication:
        arguments["operation_id"] = OPERATION_ID
    payload = await server.mcp.call_tool(name, arguments)
    _envelope(payload, name, "error")
    assert payload["issues"][0]["code"] == "runtime_plan_stale"
    public_seam.methods[f"confirm_embedding_{prefix}_v2"].assert_not_called()
    public_seam.methods[name].assert_not_called()


@pytest.mark.parametrize("publication", (False, True))
@pytest.mark.asyncio
async def test_mcp_v2_omitted_approval_never_implies_consent(public_seam: SimpleNamespace, publication: bool) -> None:
    prefix = "publication" if publication else "rebuild"
    name = f"execute_embedding_{prefix}_v2"
    public_seam.methods[f"confirm_embedding_{prefix}_v2"].side_effect = PKVRuntimeError(
        ErrorCode.CONFIRMATION_REQUIRED, "explicit approval required", stage="embedding_plan", recoverable=True,
    )
    arguments = {"plan_id": PLAN_ID}
    if publication:
        arguments["operation_id"] = OPERATION_ID
    payload = await server.mcp.call_tool(name, arguments)
    _envelope(payload, name, "error")
    assert payload["issues"][0]["code"] == "confirmation_required"
    confirm = public_seam.methods[f"confirm_embedding_{prefix}_v2"]
    if confirm.called:
        assert confirm.call_args.kwargs["approved"] is False
        if not publication:
            assert confirm.call_args.kwargs["allow_network"] is False
    public_seam.methods[name].assert_not_called()


def _pending_error() -> PKVRuntimeError:
    return PKVRuntimeError(
        ErrorCode.EMBEDDING_PUBLICATION_PENDING,
        "private source body /private/stage credential=do-not-project",
        stage="embedding_publication", recoverable=True,
        details={
            "operation_id": OPERATION_ID, "effective_state": "awaiting_publish",
            "provider_effect": "completed", "publish_only_required": True,
        },
    )


@pytest.mark.parametrize("publication", (False, True))
def test_cli_v2_pending_preserves_identity_without_private_message(
    public_seam: SimpleNamespace, publication: bool,
) -> None:
    prefix = "publication" if publication else "rebuild"
    name = f"execute_embedding_{prefix}_v2"
    error = _pending_error()
    public_seam.methods[name].side_effect = error
    arguments = ["embedding", "publish-v2", OPERATION_ID] if publication else ["embedding", "rebuild-v2", "--allow-network"]
    result = CliRunner().invoke(cli_main.cli, [*arguments, "--apply", "--confirm", PLAN_ID])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    _envelope(payload, name, "error")
    issue = payload["issues"][0]
    assert issue["details"] == error.to_dict()["details"]
    assert issue["stage"] == "embedding_publication"
    assert issue["recoverable"] is True
    assert "private" not in json.dumps(payload)


@pytest.mark.parametrize("v2", (False, True))
def test_mcp_pending_projection_retains_v1_and_v2_safe_details(v2: bool) -> None:
    error = _pending_error()
    if v2:
        payload = tools._embedding_v2_failure_payload(error, operation="execute_embedding_rebuild_v2")
        _envelope(payload, "execute_embedding_rebuild_v2", "error")
    else:
        payload = tools._embedding_rebuild_failure_payload(error)
    issue = payload["issues"][0]
    assert issue["code"] == "embedding_publication_pending"
    assert issue["stage"] == "embedding_publication"
    assert issue["recoverable"] is True
    assert issue["details"] == error.to_dict()["details"]
    assert "private" not in json.dumps(payload)


def test_cli_v1_pending_projection_retains_safe_details(monkeypatch: pytest.MonkeyPatch) -> None:
    error = _pending_error()
    monkeypatch.setattr(commands, "_load_config", Mock(side_effect=error))
    result = CliRunner().invoke(cli_main.cli, ["embedding", "rebuild"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["code"] == "embedding_publication_pending"
    assert payload["details"] == error.to_dict()["details"]
    assert "private" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("stage", "expected_stage"),
    [
        ("embedding_stage_proof", "embedding_stage_proof"),
        ("/private/stage credential=secret", "embedding_rebuild"),
    ],
)
def test_v2_cli_and_mcp_preserve_safe_stages_and_reject_malformed_stages(
    public_seam: SimpleNamespace, stage: str, expected_stage: str,
) -> None:
    error = PKVRuntimeError(
        ErrorCode.REPAIR_REQUIRED, "private source-body credential-secret",
        stage=stage, recoverable=True,
    )
    public_seam.methods["inspect_embedding_rebuild_v2"].side_effect = error
    result = CliRunner().invoke(cli_main.cli, ["embedding", "inspect-v2"])
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    cli_payload = json.loads(result.stderr)
    mcp_payload = tools._embedding_v2_failure_payload(error, operation="inspect_embedding_rebuild_v2")
    for payload in (cli_payload, mcp_payload):
        _envelope(payload, "inspect_embedding_rebuild_v2", "error")
        issue = payload["issues"][0]
        assert issue["code"] == "repair_required"
        assert issue["stage"] == expected_stage
        assert issue["recoverable"] is True
        serialized = json.dumps(payload)
        assert all(token not in serialized for token in ("private", "source-body", "credential-secret"))


@pytest.mark.parametrize("publication", (False, True))
@pytest.mark.asyncio
async def test_mcp_unexpected_failure_does_not_reflect_input_or_exception(
    public_seam: SimpleNamespace, publication: bool,
) -> None:
    name = "plan_embedding_publication_v2" if publication else "plan_embedding_rebuild_v2"
    public_seam.mcp_config.side_effect = OSError("credential-secret /private/root source-body")
    arguments = {"operation_id": OPERATION_ID} if publication else {}
    payload = await server.mcp.call_tool(name, arguments)
    _envelope(payload, name, "error")
    serialized = json.dumps(payload)
    assert all(token not in serialized for token in ("credential-secret", "/private/root", "source-body"))
