"""
CLI module initialization.

Exports the package version and lazily resolves the core CLI command group.
Do not pre-import commands: Python must be able to execute that module with
``-m`` without a duplicate-import warning corrupting its error JSON stream.
"""

from typing import Optional

from src import __version__ as __version__

_cli_import_error: Optional[Exception] = None

def __getattr__(name: str):
    if name != "cli":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _cli_import_error
    try:
        from src.cli.commands import cli
    except ModuleNotFoundError as exc:
        _cli_import_error = exc

        def cli(*args: object, **kwargs: object) -> None:
            """Fallback CLI entrypoint when commands module is missing."""
            raise ModuleNotFoundError(
                "CLI commands module 'src.cli.commands' is missing. "
                "Implement it before invoking the CLI."
            ) from _cli_import_error
    globals()[name] = cli
    return cli

__all__ = ["__version__", "cli"]
