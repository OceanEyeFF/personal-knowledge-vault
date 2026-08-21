"""Command-specific CLI readiness-gate contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.layout import RuntimeLayout
from src.runtime.lifecycle import RuntimeActionKind, RuntimeReadiness
from src.utils.config import Config


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _fresh_nested_cli_config(tmp_path: Path) -> tuple[RuntimeLayout, Config]:
    """Build a valid synthetic Config whose data root has not been created.

    ``run-test.ps1`` must pre-create its selected DataRoot for general CLI and
    pytest isolation.  Keeping this lifecycle root as an uncreated nested child
    lets the adapter regression exercise the real first-run state without
    weakening that global wrapper contract or reading a user profile.
    """

    layout = RuntimeLayout.resolve(
        resources_root=PROJECT_ROOT,
        user_data_root=tmp_path / "nested" / "fresh-runtime-root",
        profile_root=tmp_path / "synthetic-profile",
        environment={},
    )
    assert not layout.user_data_root.exists()
    layout.user_config_path.parent.mkdir(parents=True)
    layout.user_config_path.write_text(
        json.dumps(
            {
                "ai": {
                    "llm": {"api_key": "synthetic-cli-llm-key"},
                    "embedding": {
                        "api_key": "synthetic-cli-embedding-key",
                        "dim": 1536,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return layout, Config(layout=layout)


def _invoke_lifecycle_cli(
    monkeypatch: pytest.MonkeyPatch,
    config: Config,
    arguments: list[str],
):
    """Invoke the actual Click lifecycle adapter against one explicit snapshot."""

    from src.cli import commands

    monkeypatch.setattr(commands, "_load_config", lambda: config)
    result = CliRunner().invoke(commands.cli, arguments)
    return result, json.loads(result.output)


@pytest.mark.parametrize(
    "command_name",
    ("search", "show", "list", "tags", "related", "stats"),
)
def test_degraded_runtime_allows_only_explicit_read_commands(
    command_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed data remains observable without turning the read into a write."""

    from src import main as cli_main

    config = object()
    configure_application = Mock()
    configure_logging = Mock()
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(
        cli_main,
        "inspect_runtime",
        lambda supplied: SimpleNamespace(readiness=RuntimeReadiness.DEGRADED),
    )
    monkeypatch.setattr(cli_main, "configure_application", configure_application)
    monkeypatch.setattr(cli_main, "_configure_logging", configure_logging)

    cli_main._prepare_cli_runtime(command_name=command_name)

    configure_application.assert_called_once_with(config)
    configure_logging.assert_not_called()


@pytest.mark.parametrize("command_name", ("archive", "archive-text", None, "future-write"))
def test_degraded_runtime_rejects_writes_and_unknown_commands(
    command_name: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new data-root mutation still needs an explicit repair first."""

    from src import main as cli_main

    config = object()
    configure_application = Mock()
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(
        cli_main,
        "inspect_runtime",
        lambda supplied: SimpleNamespace(readiness=RuntimeReadiness.DEGRADED),
    )
    monkeypatch.setattr(cli_main, "configure_application", configure_application)

    with pytest.raises(PKVRuntimeError) as raised:
        cli_main._prepare_cli_runtime(command_name=command_name)

    assert raised.value.code is ErrorCode.REPAIR_REQUIRED
    assert raised.value.stage == "runtime_readiness"
    configure_application.assert_not_called()


def test_cli_inspect_reports_setup_plan_for_an_uncreated_nested_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle adapter preserves first-run state beneath wrapper DataRoot."""

    layout, config = _fresh_nested_cli_config(tmp_path)

    result, payload = _invoke_lifecycle_cli(monkeypatch, config, ["inspect"])

    assert result.exit_code == 0, result.output
    assert payload["status"] == "success"
    assert payload["readiness"] == RuntimeReadiness.SETUP_REQUIRED.value
    assert [action["kind"] for action in payload["plan"]["actions"]] == [
        RuntimeActionKind.VALIDATE_PROVIDERS.value,
        RuntimeActionKind.INITIALIZE_FRESH.value,
        RuntimeActionKind.RECORD_RUNTIME_SNAPSHOT.value,
    ]
    assert payload["plan"]["plan_id"]
    assert not layout.user_data_root.exists()


@pytest.mark.parametrize("command_name", ("setup", "repair"))
def test_cli_lifecycle_plan_only_commands_do_not_materialize_a_fresh_root(
    command_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both plan-only commands remain read-only for first-run inspection."""

    layout, config = _fresh_nested_cli_config(tmp_path)

    result, payload = _invoke_lifecycle_cli(monkeypatch, config, [command_name])

    assert result.exit_code == 0, result.output
    assert payload["readiness"] == RuntimeReadiness.SETUP_REQUIRED.value
    assert payload["plan"]["plan_id"]
    assert RuntimeActionKind.INITIALIZE_FRESH.value in {
        action["kind"] for action in payload["plan"]["actions"]
    }
    assert not layout.user_data_root.exists()


def test_cli_setup_apply_requires_exact_plan_and_network_confirmation_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bad/missing confirmation never reaches the writer or Provider boundary."""

    import src.runtime.lifecycle as lifecycle_module

    layout, config = _fresh_nested_cli_config(tmp_path)
    plan_result, plan_payload = _invoke_lifecycle_cli(monkeypatch, config, ["setup"])
    assert plan_result.exit_code == 0, plan_result.output
    plan_id = plan_payload["plan"]["plan_id"]
    provider_constructions: list[object] = []

    class UnexpectedProviderProbe:
        def __init__(self, *args: object, **kwargs: object) -> None:
            provider_constructions.append((args, kwargs))

    monkeypatch.setattr(lifecycle_module, "LiveProviderProbe", UnexpectedProviderProbe)

    missing_confirm, missing_confirm_payload = _invoke_lifecycle_cli(
        monkeypatch,
        config,
        ["setup", "--apply"],
    )
    assert missing_confirm.exit_code == 1, missing_confirm.output
    assert missing_confirm_payload["code"] == ErrorCode.CONFIRMATION_REQUIRED.value

    stale_confirm, stale_confirm_payload = _invoke_lifecycle_cli(
        monkeypatch,
        config,
        ["setup", "--apply", "--confirm", "not-the-current-plan"],
    )
    assert stale_confirm.exit_code == 1, stale_confirm.output
    assert stale_confirm_payload["code"] == ErrorCode.RUNTIME_PLAN_STALE.value

    missing_network, missing_network_payload = _invoke_lifecycle_cli(
        monkeypatch,
        config,
        ["setup", "--apply", "--confirm", plan_id],
    )
    assert missing_network.exit_code == 1, missing_network.output
    assert missing_network_payload["code"] == ErrorCode.CONFIRMATION_REQUIRED.value
    assert provider_constructions == []
    assert not layout.user_data_root.exists()


def test_cli_setup_apply_forwards_explicit_network_consent_to_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter forwards an operator's network consent without doing I/O here."""

    from src.cli import commands

    layout, config = _fresh_nested_cli_config(tmp_path)
    plan_result, plan_payload = _invoke_lifecycle_cli(monkeypatch, config, ["setup"])
    assert plan_result.exit_code == 0, plan_result.output
    plan_id = plan_payload["plan"]["plan_id"]
    captured: dict[str, object] = {}

    def fake_execute(plan, confirmation, *, writer_lease_factory):
        captured["plan"] = plan
        captured["confirmation"] = confirmation
        captured["writer_lease_factory"] = writer_lease_factory
        return SimpleNamespace(
            inspection=plan.inspection,
            to_dict=lambda: {"status": "synthetic-confirmed"},
        )

    monkeypatch.setattr(commands, "execute_runtime_plan", fake_execute)
    result, payload = _invoke_lifecycle_cli(
        monkeypatch,
        config,
        ["setup", "--apply", "--confirm", plan_id, "--allow-network"],
    )

    assert result.exit_code == 0, result.output
    assert captured["confirmation"].plan_id == plan_id
    assert captured["confirmation"].allow_network is True
    assert callable(captured["writer_lease_factory"])
    assert payload["execution"] == {"status": "synthetic-confirmed"}
    assert not layout.user_data_root.exists()
