"""CLI entrypoint for Personal Knowledge Vault."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from functools import wraps
from pathlib import Path

import click

# Ensure project root is on sys.path for "python src/main.py" usage.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import __version__
from src.utils.config import Config, get_config
from src.utils.logger import LoggerSetup
from src.runtime.bootstrap import project_bootstrap_error
from src.runtime.errors import PKVRuntimeError
from src.runtime.file_logging import runtime_file_log_binding
from src.runtime.lifecycle import RuntimeInspection, RuntimeReadiness, inspect_runtime
from src.application import configure_application

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
    "inspect": "inspect",
    "setup": "setup",
    "repair": "repair",
    "stats": "stats",
}

# These commands deliberately inspect or update only the user configuration.
# They must not make the historical implicit bootstrap path observable before a
# user has reviewed and confirmed a lifecycle plan.
_LIFECYCLE_COMMANDS = frozenset({"inspect", "setup", "repair"})
_CONFIG_COMMANDS = frozenset({"config"})
_BUSINESS_COMMANDS = frozenset(COMMAND_ATTRS) - _LIFECYCLE_COMMANDS - _CONFIG_COMMANDS
# Keep this list explicit and fail closed for future commands.  A degraded
# runtime can expose already-committed data, but it must never accept another
# mutation until the operator has reviewed and completed its repair plan.
_READONLY_DEGRADED_COMMANDS = frozenset(
    {"search", "show", "list", "tags", "related", "stats"}
)


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
        if command is not None and cmd_name in _BUSINESS_COMMANDS:
            _attach_runtime_gate(command, command_name=cmd_name)
        return command


def _attach_runtime_gate(command: click.Command, *, command_name: str) -> None:
    """Run the command-specific readiness check after Click has parsed it.

    ``Group`` callbacks run before a subcommand has parsed its own eager
    ``--help`` option or validated required arguments.  Doing runtime work
    there turned a harmless help/validation request into a ``setup_required``
    failure.  Wrapping the already-created command callback leaves Click in
    charge of parsing and only gates an actual command execution.
    """

    if getattr(command, "_pkv_runtime_gate_attached", False):
        return
    callback = command.callback
    if callback is None:
        return

    @wraps(callback)
    def _gated_callback(*args: object, **kwargs: object) -> object:
        _prepare_cli_runtime(command_name=command_name)
        return callback(*args, **kwargs)

    command.callback = _gated_callback
    setattr(command, "_pkv_runtime_gate_attached", True)


def _resolve_log_level(verbose: bool, debug: bool) -> str:
    """Map CLI flags to a logging level string."""
    if debug:
        return LOG_LEVEL_DEBUG
    if verbose:
        return LOG_LEVEL_INFO
    return LOG_LEVEL_WARNING


def _configure_logging(
    config: Config,
    level: str,
    *,
    snapshot_id: str | None = None,
) -> None:
    """Configure logging only after the runtime has been verified READY.

    The persistent ``pkv.log`` handler is delayed and only emits while the
    current task already owns a supported data-root mutation lease.  Startup
    and read-only operations remain stderr-only, so logging cannot turn a read
    into an unleased runtime write.
    """

    stage = "runtime_logging"
    try:
        log_file = config.log_dir / "pkv.log"
        binding = runtime_file_log_binding(
            config,
            snapshot_id=snapshot_id or f"config-{id(config)}",
        )
        LoggerSetup.setup(
            level=level,
            log_file=log_file,
            console_stream=sys.stderr,
            runtime_file_binding=binding,
        )
        logging.getLogger(__name__).debug("Logging initialized at %s", level)
    except PKVRuntimeError:
        raise
    except Exception as exc:
        raise _StartupProjectionError(
            project_bootstrap_error(exc, adapter="cli", stage=stage)
        ) from None


def _readiness_error(inspection: RuntimeInspection) -> PKVRuntimeError:
    """Project a non-ready inspection into one safe, stable startup error."""

    if inspection.readiness is RuntimeReadiness.SETUP_REQUIRED:
        code = "SETUP_REQUIRED"
        message = "知识库尚未完成显式初始化。"
    elif inspection.readiness is RuntimeReadiness.UPGRADE_REQUIRED:
        code = "DATABASE_UPGRADE_REQUIRED"
        message = "知识库需要显式升级后才能使用。"
    else:
        # ``degraded`` reaches this branch for writes and unknown commands;
        # confirmed repair remains required before another mutation.
        code = "REPAIR_REQUIRED"
        message = "知识库需要先完成显式修复。"
    from src.runtime.errors import ErrorCode

    return PKVRuntimeError(
        getattr(ErrorCode, code),
        message,
        stage="runtime_readiness",
        recoverable=True,
    )


def _prepare_cli_runtime(*, command_name: str | None = None) -> None:
    """Configure one business-command process after successful Click parsing.

    Lifecycle and user-config commands intentionally bypass this function: they
    may inspect or repair an unready root without implicit initialization.  A
    business command obtains the same immutable ``Config`` snapshot for the
    readiness inspection and application graph.  Only explicitly listed
    read-only commands may use a ``DEGRADED`` snapshot; every mutation and any
    unknown future command remains fail-closed until the runtime is ``READY``.
    """

    context = click.get_current_context(silent=True)
    root_context = context.find_root() if context is not None else None
    context_object = root_context.obj if root_context is not None else None
    try:
        config = (
            context_object.get("config")
            if isinstance(context_object, dict) and context_object.get("config") is not None
            else get_config()
        )
        if isinstance(context_object, dict):
            context_object["config"] = config
        inspection = inspect_runtime(config)
        degraded_read = (
            inspection.readiness is RuntimeReadiness.DEGRADED
            and command_name in _READONLY_DEGRADED_COMMANDS
        )
        if inspection.readiness is not RuntimeReadiness.READY and not degraded_read:
            raise _readiness_error(inspection)

        # Application composition itself is lazy and has no bootstrap/recovery
        # side effect.  This lets a degraded runtime expose safe reads of data
        # that was already committed, while write commands stay above.  Do not
        # set up the file logger for that read-only path: opening ``pkv.log``
        # would turn a recovery-safe read into an unleased data-root mutation.
        configure_application(config)
        if inspection.readiness is RuntimeReadiness.READY:
            verbose = bool(context_object.get("verbose")) if isinstance(context_object, dict) else False
            debug = bool(context_object.get("debug")) if isinstance(context_object, dict) else False
            _configure_logging(
                config,
                _resolve_log_level(verbose, debug),
                snapshot_id=f"config-{id(config)}",
            )
    except _StartupProjectionError:
        raise
    except PKVRuntimeError:
        raise
    except Exception as exc:
        raise _StartupProjectionError(
            project_bootstrap_error(exc, adapter="cli", stage="runtime_configuration")
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
    # Do not inspect/configure a runtime here.  A group callback fires before
    # Click has processed subcommand ``--help`` or parameter validation.  The
    # selected business command receives the readiness gate in
    # ``_attach_runtime_gate`` after successful parsing instead.


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
