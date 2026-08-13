"""CLI entrypoint for Personal Knowledge Vault."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path

import click

# Ensure project root is on sys.path for "python src/main.py" usage.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import __version__
from src.utils.config import get_config
from src.utils.logger import LoggerSetup
from src.runtime.bootstrap import bootstrap_runtime, project_bootstrap_error
from src.runtime.errors import PKVRuntimeError

LOG_LEVEL_DEBUG = "DEBUG"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARNING = "WARNING"

COMMAND_ATTRS = {
    "archive": "archive",
    "archive-text": "archive_text",
    "search": "search",
    "show": "show",
    "list": "list_entries",
    "tags": "tags",
    "related": "related",
    "config": "config_cmd",
    "stats": "stats",
}


class _StartupProjectionError(RuntimeError):
    """Carry an already-sanitized startup failure across Click's callback."""

    def __init__(self, payload: dict[str, object]) -> None:
        super().__init__("CLI startup failed")
        self.payload = dict(payload)


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
    stage = "runtime_configuration"
    try:
        config = get_config()
        stage = "runtime_bootstrap"
        bootstrap_runtime(config)
        stage = "runtime_logging"
        log_file = config.log_dir / "pkv.log"
        LoggerSetup.setup(
            level=level,
            log_file=log_file,
            path_validator=config.layout.writable_user_path,
            console_stream=sys.stderr,
        )
        logging.getLogger(__name__).debug("Logging initialized at %s", level)
    except PKVRuntimeError:
        raise
    except Exception as exc:
        raise _StartupProjectionError(
            project_bootstrap_error(exc, adapter="cli", stage=stage)
        ) from None


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
    try:
        cli()
    except _StartupProjectionError as exc:
        sys.stderr.write(
            json.dumps(
                exc.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        raise SystemExit(1) from None
    except PKVRuntimeError as exc:
        payload = project_bootstrap_error(exc, adapter="cli")
        sys.stderr.write(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
