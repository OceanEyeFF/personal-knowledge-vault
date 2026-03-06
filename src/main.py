"""CLI entrypoint for Personal Knowledge Vault."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

import click

# Ensure project root is on sys.path for "python src/main.py" usage.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.config import get_config
from src.utils.logger import LoggerSetup

__version__ = "0.8.0-alpha"

LOG_LEVEL_DEBUG = "DEBUG"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARNING = "WARNING"

COMMAND_ATTRS = {
    "archive": "archive",
    "search": "search",
    "show": "show",
    "list": "list_entries",
    "config": "config_cmd",
    "stats": "stats",
}


class LazyCLIGroup(click.Group):
    """Lazily import subcommands so `--version` does not pull heavy deps."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(COMMAND_ATTRS.keys())

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        attr_name = COMMAND_ATTRS.get(cmd_name)
        if not attr_name:
            return None

        module = importlib.import_module("src.cli.commands")
        command = getattr(module, attr_name, None)
        if command is not None and cmd_name == "list":
            command.name = "list"
        if command is not None and cmd_name == "config":
            command.name = "config"
        return command


def _resolve_log_level(verbose: bool, debug: bool) -> str:
    """Map CLI flags to a logging level string."""
    if debug:
        return LOG_LEVEL_DEBUG
    if verbose:
        return LOG_LEVEL_INFO
    return LOG_LEVEL_WARNING


def _configure_logging(level: str) -> None:
    """Configure global logging with project defaults."""
    config = get_config()
    log_file = config.log_dir / "pkv.log"
    LoggerSetup.setup(level=level, log_file=log_file)
    logging.getLogger(__name__).debug("Logging initialized at %s", level)


@click.group(cls=LazyCLIGroup)
@click.option("--verbose", is_flag=True, help="Enable verbose output (INFO).")
@click.option("--debug", is_flag=True, help="Enable debug output (DEBUG).")
@click.version_option(__version__, prog_name="pkv")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, debug: bool) -> None:
    """Personal Knowledge Vault command line interface."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["debug"] = debug
    _configure_logging(_resolve_log_level(verbose, debug))


def main() -> None:
    """CLI entrypoint."""
    cli()


if __name__ == "__main__":
    main()
