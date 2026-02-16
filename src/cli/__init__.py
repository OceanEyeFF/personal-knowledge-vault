"""
CLI module initialization.

Exports the package version and the core CLI command group for reuse by
entrypoints such as src/main.py.
"""

from typing import Optional

from src import __version__ as __version__

_cli_import_error: Optional[Exception] = None

try:
    from src.cli.commands import cli as cli
except ModuleNotFoundError as exc:
    _cli_import_error = exc

    def cli(*args: object, **kwargs: object) -> None:
        """Fallback CLI entrypoint when commands module is missing."""
        raise ModuleNotFoundError(
            "CLI commands module 'src.cli.commands' is missing. "
            "Implement it before invoking the CLI."
        ) from _cli_import_error

__all__ = ["__version__", "cli"]
