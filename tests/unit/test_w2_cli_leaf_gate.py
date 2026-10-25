"""CLI-01..04: parsing, full-path policies, and one leaf runtime snapshot."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner

from src import main as cli_main
from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.runtime.lifecycle import RuntimeReadiness


def _install_tree(monkeypatch: pytest.MonkeyPatch, commands: dict[str, click.Command]) -> None:
    """Supply Click commands through the real lazy root without product I/O."""

    module = SimpleNamespace(**commands)
    monkeypatch.setattr(cli_main, "COMMAND_ATTRS", {name: name for name in commands})
    monkeypatch.setattr(cli_main, "importlib", SimpleNamespace(import_module=lambda _: module))


@pytest.fixture
def runtime_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    config = object()
    boundaries = {
        "get_config": Mock(return_value=config),
        "inspect_runtime": Mock(return_value=SimpleNamespace(readiness=RuntimeReadiness.READY)),
        "configure_application": Mock(),
        "_configure_logging": Mock(),
    }
    for name, callback in boundaries.items():
        monkeypatch.setattr(cli_main, name, callback)
    return boundaries


def _nested_tree(callback: object) -> click.Group:
    leaf = click.Command(
        "get",
        params=[click.Argument(["topic_id"]), click.Option(["--limit"], type=int, default=10)],
        callback=callback,
    )
    return click.Group("outcome", commands={"nested": click.Group("nested", commands={"get": leaf})})


@pytest.mark.parametrize(
    ("arguments", "exit_code", "fragment"),
    [
        (("--help",), 0, "Commands:"),
        (("--version",), 0, "version"),
        (("outcome", "--help"), 0, "Commands:"),
        (("outcome", "nested", "--help"), 0, "Commands:"),
        (("outcome", "nested", "get", "--help"), 0, "TOPIC_ID"),
        (("outcome", "nested", "get", "--version"), 2, "No such option"),
        (("outcome", "nested", "get"), 2, "Missing argument"),
        (("outcome", "nested", "get", "topic", "--limit", "bad"), 2, "Invalid value"),
        (("outcome", "nested", "get", "--unknown"), 2, "No such option"),
        (("unknown",), 2, "No such command"),
        (("outcome", "unknown"), 2, "No such command"),
        (("outcome", "nested", "unknown"), 2, "No such command"),
        (("outcome", "nested"), 2, "Commands:"),
    ],
)
def test_deep_help_and_validation_never_load_even_a_broken_config(
    monkeypatch: pytest.MonkeyPatch,
    runtime_calls: dict[str, Mock],
    arguments: tuple[str, ...],
    exit_code: int,
    fragment: str,
) -> None:
    """CLI-01/02/04: eager parsing works even if loading Config would fail."""

    leaf = Mock()
    _install_tree(monkeypatch, {"outcome": _nested_tree(leaf)})
    runtime_calls["get_config"].side_effect = ValueError("synthetic damaged user configuration")

    result = CliRunner().invoke(cli_main.cli, arguments)

    assert result.exit_code == exit_code, result.output
    assert fragment in result.output
    leaf.assert_not_called()
    for boundary in runtime_calls.values():
        boundary.assert_not_called()


@pytest.mark.parametrize("root_name", ("outcome", "config", "embedding"))
def test_unclassified_leaf_fails_closed_after_successful_parsing(
    monkeypatch: pytest.MonkeyPatch, runtime_calls: dict[str, Mock], root_name: str
) -> None:
    """CLI-03: ancestors cannot grant an unregistered leaf a business policy."""

    leaf = Mock()
    _install_tree(monkeypatch, {root_name: _nested_tree(leaf)})

    result = CliRunner().invoke(cli_main.cli, (root_name, "nested", "get", "topic"))

    assert result.exit_code == 1
    assert "no registered runtime policy" in result.output
    leaf.assert_not_called()
    for boundary in runtime_calls.values():
        boundary.assert_not_called()


@pytest.mark.parametrize("policy", ("read", "mutation"))
@pytest.mark.parametrize(
    "readiness", (RuntimeReadiness.READY, RuntimeReadiness.DEGRADED, RuntimeReadiness.SETUP_REQUIRED)
)
def test_deep_leaf_uses_its_policy_and_one_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    runtime_calls: dict[str, Mock],
    policy: str,
    readiness: RuntimeReadiness,
) -> None:
    """CLI-03: nested reads stay READY-only unless individually allowed."""

    from src.cli.commands import _load_config

    seen_configs = []
    leaf = Mock(side_effect=lambda **_: seen_configs.append(_load_config()))
    _install_tree(monkeypatch, {"outcome": _nested_tree(leaf)})
    monkeypatch.setattr(cli_main, "_COMMAND_POLICIES", {"outcome nested get": policy})
    runtime_calls["inspect_runtime"].return_value = SimpleNamespace(readiness=readiness)

    result = CliRunner().invoke(cli_main.cli, ("outcome", "nested", "get", "topic"))

    config = runtime_calls["get_config"].return_value
    runtime_calls["get_config"].assert_called_once_with()
    runtime_calls["inspect_runtime"].assert_called_once_with(config)
    if readiness is RuntimeReadiness.READY:
        assert result.exit_code == 0, result.exception
        leaf.assert_called_once_with(topic_id="topic", limit=10)
        assert seen_configs == [config]
        runtime_calls["configure_application"].assert_called_once_with(config)
        runtime_calls["_configure_logging"].assert_called_once_with(
            config, "WARNING", snapshot_id=f"config-{id(config)}"
        )
    else:
        assert isinstance(result.exception, PKVRuntimeError)
        assert result.exception.code is (
            ErrorCode.REPAIR_REQUIRED if readiness is RuntimeReadiness.DEGRADED else ErrorCode.SETUP_REQUIRED
        )
        leaf.assert_not_called()
        runtime_calls["configure_application"].assert_not_called()
        runtime_calls["_configure_logging"].assert_not_called()


def test_shared_command_resolves_each_invocation_path_before_degraded_access(
    monkeypatch: pytest.MonkeyPatch, runtime_calls: dict[str, Mock]
) -> None:
    """A top-level read alias cannot lend its DEGRADED grant to another path."""

    callback = Mock()
    shared = click.Command("search", callback=callback)
    _install_tree(monkeypatch, {"search": shared, "outcome": click.Group(commands={"search": shared})})
    monkeypatch.setattr(cli_main, "_COMMAND_POLICIES", {"search": "read", "outcome search": "mutation"})
    runtime_calls["inspect_runtime"].return_value = SimpleNamespace(readiness=RuntimeReadiness.DEGRADED)

    allowed = CliRunner().invoke(cli_main.cli, ("search",), prog_name="program with spaces")
    rejected = CliRunner().invoke(cli_main.cli, ("outcome", "search"), prog_name="program with spaces")

    assert allowed.exit_code == 0, allowed.exception
    assert isinstance(rejected.exception, PKVRuntimeError)
    assert rejected.exception.code is ErrorCode.REPAIR_REQUIRED
    callback.assert_called_once_with()
    runtime_calls["configure_application"].assert_called_once()
    runtime_calls["_configure_logging"].assert_not_called()


@pytest.mark.parametrize(
    "path",
    ("inspect", "setup", "repair", "embedding inspect", "embedding rebuild", "config show", "config get", "config set"),
)
def test_special_leaves_keep_their_own_contract_without_business_ready_gate(
    monkeypatch: pytest.MonkeyPatch, runtime_calls: dict[str, Mock], path: str
) -> None:
    """Lifecycle/config policy exemptions belong to the registered leaf only."""

    parts = path.split()
    callback = Mock()
    command = click.Command(parts[-1], callback=callback)
    root_command = command if len(parts) == 1 else click.Group(parts[0], commands={parts[1]: command})
    _install_tree(monkeypatch, {parts[0]: root_command})

    result = CliRunner().invoke(cli_main.cli, parts)

    assert result.exit_code == 0, result.exception
    callback.assert_called_once_with()
    for boundary in runtime_calls.values():
        boundary.assert_not_called()


def test_all_product_leaves_have_explicit_policies_and_degraded_reads_stay_narrow() -> None:
    """CLI-03: new command-tree leaves require an explicit policy decision."""

    from src.cli import commands

    paths = set()

    def visit(command: click.Command, path: str) -> None:
        if isinstance(command, click.Group):
            for name, child in command.commands.items():
                visit(child, f"{path} {name}")
        else:
            paths.add(path)

    for name, attr in cli_main.COMMAND_ATTRS.items():
        visit(getattr(commands, attr), name)

    assert paths == set(cli_main._COMMAND_POLICIES)
    assert set(cli_main._COMMAND_POLICIES.values()) == {"read", "mutation", "lifecycle", "config"}
    assert cli_main._READONLY_DEGRADED_COMMANDS == {"search", "show", "list", "tags", "related", "stats"}
    for path in ("outcome get", "outcome list", "outcome suggest-evidence"):
        assert cli_main._COMMAND_POLICIES[path] == "read"
        assert path not in cli_main._READONLY_DEGRADED_COMMANDS
