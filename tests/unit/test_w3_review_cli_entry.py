"""Module entry parsing does not read even a broken runtime configuration."""

from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import runpy
import sys
from unittest.mock import Mock

import pytest

from src import main as cli_main


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("arguments", "exit_code"),
    [
        (("--version",), 0),
        (("outcome", "approve-v2", "--help"), 0),
        (("outcome", "approve-v2", "brief-a", "--expected-revision", "bad"), 2),
        (("outcome", "get-v2", "topic-a", "--unknown"), 2),
    ],
)
def test_module_parse_never_reads_broken_config(
    monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...], exit_code: int,
) -> None:
    boundaries = {
        name: Mock(side_effect=AssertionError(f"parse-only invocation reached {name}"))
        for name in ("get_config", "inspect_runtime", "configure_application", "_configure_logging")
    }
    for name, callback in boundaries.items():
        monkeypatch.setattr(cli_main, name, callback)
    # Run a fresh __main__ module; never reuse a command tree wrapped by a prior
    # standard-entry invocation in this pytest process.
    monkeypatch.setattr(sys, "argv", ["src.cli.commands", *arguments])
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(PROJECT_ROOT / "src/cli/commands.py"), run_name="__main__")
    assert exited.value.code == exit_code
    for callback in boundaries.values():
        callback.assert_not_called()


def test_package_cli_export_keeps_registered_click_group() -> None:
    from src.cli import cli
    from src.cli.commands import cli as command_group

    assert cli is command_group
    assert "outcome" in cli.commands


def test_package_cli_export_preserves_missing_module_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__
    missing = ModuleNotFoundError("synthetic missing command dependency", name="synthetic_dependency")

    def import_without_commands(name, *args, **kwargs):
        if name == "src.cli.commands":
            raise missing
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_commands)
    spec = importlib.util.spec_from_file_location("_cli_package_probe", PROJECT_ROOT / "src/cli/__init__.py")
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(package)
    with pytest.raises(ModuleNotFoundError, match="CLI commands module") as raised:
        package.cli()
    assert raised.value.__cause__ is missing
