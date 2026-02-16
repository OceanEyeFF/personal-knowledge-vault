"""CLI entrypoint for Personal Knowledge Vault."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

# Ensure project root is on sys.path for "python src/main.py" usage.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.cli.commands import archive, config_cmd, list_entries as list_cmd, search, show, stats
from src.utils.config import get_config
from src.utils.logger import LoggerSetup

__version__ = "0.6.0"

LOG_LEVEL_DEBUG = "DEBUG"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARNING = "WARNING"


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


def _register_commands(group: click.Group) -> None:
    """Attach core commands to the CLI group."""
    group.add_command(archive)
    group.add_command(search)
    group.add_command(show)
    group.add_command(list_cmd, name="list")
    group.add_command(config_cmd, name="config")
    group.add_command(stats)


@click.group()
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


_register_commands(cli)


def main() -> None:
    """CLI entrypoint."""
    cli()


if __name__ == "__main__":
    main()
