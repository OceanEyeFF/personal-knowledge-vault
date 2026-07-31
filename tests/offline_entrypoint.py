"""Dedicated base-config entrypoint for offline child-process tests."""

from __future__ import annotations

import os
import platform
import re
import runpy
import stat
import sys
from pathlib import Path
from typing import Callable

from tests.offline_runtime import (
    LOAD_LOCAL_SENTINEL,
    LIVE_ENV_KEYS,
    OFFLINE_SENTINEL,
    PROJECT_ROOT_SENTINEL,
    RUNTIME_PATH_ENV_KEYS,
    assert_config_runtime_paths,
    install_offline_network_guard,
    install_offline_process_guard,
    mark_offline_runtime_ready,
    scrub_child_process_env,
    validate_test_runtime_paths,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "local.yaml"


def _live_mode_enabled() -> bool:
    load_local = os.environ.get(LOAD_LOCAL_SENTINEL) == "1"
    run_live = os.environ.get("PKV_RUN_LIVE") == "1"
    offline = os.environ.get(OFFLINE_SENTINEL) == "1"
    if load_local and run_live and not offline:
        return True
    if not load_local and not run_live and offline:
        return False
    raise RuntimeError("child environment live/offline sentinels are inconsistent")


def _validate_child_environment() -> None:
    expected_lexical = Path(
        os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT)))
    )
    configured_root = os.environ.get(PROJECT_ROOT_SENTINEL)
    if configured_root is None or not Path(configured_root).is_absolute():
        raise RuntimeError("child project-root sentinel is missing or invalid")
    configured_lexical = Path(
        os.path.abspath(os.path.normpath(configured_root))
    )
    if os.path.normcase(os.fspath(configured_lexical)) != os.path.normcase(
        os.fspath(expected_lexical)
    ):
        raise RuntimeError("child project-root sentinel is missing or invalid")

    expected_root = expected_lexical.resolve()
    if configured_lexical.resolve() != expected_root:
        raise RuntimeError("child project-root sentinel is missing or invalid")

    runtime_overrides = {
        key: os.environ[key]
        for key in RUNTIME_PATH_ENV_KEYS
        if key in os.environ
    }
    canonical = validate_test_runtime_paths(
        project_root=expected_root,
        runtime_overrides=runtime_overrides,
    )
    os.environ.update(canonical)


def _install_test_config(*, load_local: bool) -> Callable[..., object]:
    """Install Config before importing either product entrypoint."""

    from src.utils import config as config_module

    def new_config(*_args, **_kwargs):
        local_path = str(LOCAL_CONFIG_PATH) if load_local else None
        return config_module.Config(str(BASE_CONFIG_PATH), local_path)

    config = new_config()
    assert_config_runtime_paths(
        config,
        {key: os.environ[key] for key in RUNTIME_PATH_ENV_KEYS},
    )
    config.ensure_dirs()
    config_module._config_instance = config

    def validated_config_factory(*_args, **_kwargs):
        return config

    return validated_config_factory


def _bind_test_config_factory(config_factory: Callable[..., object]) -> None:
    """Make later direct ``Config()`` imports return the validated singleton."""

    from src.utils import config as config_module

    # This mutation is process-local. The dedicated child exits after the
    # requested target, so no production process observes the test factory.
    config_module.Config = config_factory


def _run_cli(config_factory: Callable[..., object]) -> None:
    import src.cli.commands as commands_module

    # commands.py imports Config directly, so patch that binding as well as the
    # shared config singleton before importing the lazy CLI entrypoint.
    commands_module.Config = config_factory

    from src.main import main

    main()


def _run_mcp() -> None:
    from src.mcp.server import main

    main()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


_PROHIBITED_DIRECT_ROOT_NAMES = frozenset(
    {".data", ".data-test", ".git", "config"}
)


def _direct_candidate_lexical(value: str) -> tuple[Path, Path, Path]:
    """Validate a Direct Python path lexically without touching its target."""

    root_lexical = Path(
        os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT)))
    )
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root_lexical / candidate
    candidate_lexical = Path(
        os.path.abspath(os.path.normpath(os.fspath(candidate)))
    )
    if not _is_relative_to(candidate_lexical, root_lexical):
        raise SystemExit("direct Python script must remain inside the project root")

    relative = candidate_lexical.relative_to(root_lexical)
    if (
        not relative.parts
        or relative.parts[0].casefold() in _PROHIBITED_DIRECT_ROOT_NAMES
    ):
        raise SystemExit("direct Python script is not an executable repository target")
    if candidate_lexical.suffix.lower() != ".py":
        raise SystemExit("direct Python script must be a .py file")
    return root_lexical, candidate_lexical, relative


def _lstat_direct_candidate_chain(
    *,
    root_lexical: Path,
    relative: Path,
    allow_missing: bool,
) -> os.stat_result | None:
    """Reject links component-by-component before any operation can follow them."""

    current = root_lexical
    parts = relative.parts
    for index, part in enumerate(parts):
        current /= part
        try:
            item_stat = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            if allow_missing:
                return None
            raise SystemExit("direct Python script does not exist or is not readable")
        except OSError as exc:
            raise SystemExit(
                "direct Python script does not exist or is not readable"
            ) from exc

        file_attributes = getattr(item_stat, "st_file_attributes", 0)
        is_reparse_point = bool(file_attributes & 0x400)
        if stat.S_ISLNK(item_stat.st_mode) or is_reparse_point:
            raise SystemExit(
                "direct Python target path must not contain a symlink or reparse point"
            )
        is_leaf = index == len(parts) - 1
        if not is_leaf and not stat.S_ISDIR(item_stat.st_mode):
            if allow_missing:
                return None
            raise SystemExit("direct Python script does not exist or is not readable")

    if stat.S_ISREG(item_stat.st_mode) and item_stat.st_nlink > 1:
        raise SystemExit("direct Python script must not be a hard-linked file")
    return item_stat


def _validated_direct_script_candidate(
    value: str,
    *,
    allow_missing: bool,
) -> Path | None:
    """Resolve one candidate only after a no-follow component-chain check."""

    root_lexical, candidate_lexical, relative = _direct_candidate_lexical(value)
    item_stat = _lstat_direct_candidate_chain(
        root_lexical=root_lexical,
        relative=relative,
        allow_missing=allow_missing,
    )
    if item_stat is None:
        return None
    if not stat.S_ISREG(item_stat.st_mode):
        if allow_missing:
            return None
        raise SystemExit("direct Python script does not exist or is not readable")

    try:
        resolved = candidate_lexical.resolve(strict=True)
    except OSError as exc:
        raise SystemExit("direct Python script does not exist or is not readable") from exc
    root = root_lexical.resolve()
    if not _is_relative_to(resolved, root):
        raise SystemExit("direct Python script escaped the project root")
    resolved_relative = resolved.relative_to(root)
    if (
        not resolved_relative.parts
        or resolved_relative.parts[0].casefold()
        in _PROHIBITED_DIRECT_ROOT_NAMES
    ):
        raise SystemExit("direct Python script resolved into a prohibited project root")
    if resolved == Path(__file__).resolve():
        raise SystemExit("recursive offline entrypoint execution is not allowed")
    return resolved


def _validated_direct_script(value: str) -> Path:
    """Resolve a repository Python script without probing rejected locations."""

    resolved = _validated_direct_script_candidate(value, allow_missing=False)
    if resolved is None:  # pragma: no cover - allow_missing=False is exhaustive
        raise AssertionError("required Direct Python script unexpectedly disappeared")
    return resolved


def _try_validated_direct_script(value: str | os.PathLike[str]) -> Path | None:
    """Safely probe a conventional module candidate without following links."""

    return _validated_direct_script_candidate(
        os.fspath(value),
        allow_missing=True,
    )


_MODULE_NAME_PATTERN = re.compile(
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$",
    flags=re.ASCII,
)


def _prefer_project_root_on_sys_path() -> None:
    """Make validated repository modules win over ``tests/`` shadows."""

    root = str(PROJECT_ROOT.resolve())
    root_key = os.path.normcase(os.path.abspath(os.path.normpath(root)))
    retained: list[str] = []
    for entry in sys.path:
        try:
            value = os.getcwd() if entry == "" else os.fspath(entry)
            entry_key = os.path.normcase(os.path.abspath(os.path.normpath(value)))
        except TypeError:
            retained.append(entry)
            continue
        if entry_key != root_key:
            retained.append(entry)
    sys.path[:] = [root, *retained]


def _validated_direct_module(value: str) -> str:
    """Require a conventional repository package/module without importing it."""

    if not _MODULE_NAME_PATTERN.fullmatch(value):
        raise SystemExit("direct Python module name is invalid")
    if value == "tests.offline_entrypoint":
        raise SystemExit("recursive offline entrypoint execution is not allowed")

    parts = value.split(".")
    parent = PROJECT_ROOT
    for part in parts[:-1]:
        parent /= part
        init_script = parent / "__init__.py"
        if _try_validated_direct_script(init_script) is None:
            raise SystemExit("direct Python module is not an executable repository target")

    leaf = parent / parts[-1]
    module_script = leaf.with_suffix(".py")
    package_init = leaf / "__init__.py"
    package_main = leaf / "__main__.py"
    # PathFinder resolves a same-named package before ``module.py`` within one
    # sys.path entry, so validation must use the same precedence as runpy.
    if _try_validated_direct_script(package_init) is not None:
        if _try_validated_direct_script(package_main) is None:
            raise SystemExit(
                "direct Python module is not an executable repository target"
            )
    elif _try_validated_direct_script(module_script) is None:
        raise SystemExit("direct Python module is not an executable repository target")
    return value


_PYTEST_UNSAFE_EXACT_OPTIONS = frozenset(
    {
        "--boxed",
        "--collect-in-virtualenv",
        "--config-file",
        "--confcutdir",
        "--cov-config",
        "--debug",
        "--deselect",
        "--dist",
        "--doctest-glob",
        "--doctest-modules",
        "--forked",
        "--html",
        "--ignore",
        "--ignore-glob",
        "--json-report-file",
        "--junit-xml",
        "--junitxml",
        "--log-file",
        "--noconftest",
        "--numprocesses",
        "--override-ini",
        "--pastebin",
        "--pdbcls",
        "--plugins",
        "--pyargs",
        "--result-log",
        "--rootdir",
        "--rsyncdir",
        "--tx",
        "-c",
        "-n",
        "-o",
        "-p",
    }
)
_PYTEST_UNSAFE_LONG_PREFIXES = tuple(
    f"{name}="
    for name in _PYTEST_UNSAFE_EXACT_OPTIONS
    if name.startswith("--")
)
_PYTEST_VALUE_OPTIONS = frozenset(
    {
        "--assert",
        "--asyncio-mode",
        "--capture",
        "--code-highlight",
        "--color",
        "--cov-context",
        "--cov-fail-under",
        "--durations",
        "--durations-min",
        "--import-mode",
        "--log-cli-level",
        "--maxfail",
        "--randomly-seed",
        "--show-capture",
        "--tb",
        "--timeout",
        "--timeout-method",
        "--verbosity",
        "-k",
        "-m",
        "-r",
    }
)
_PYTEST_TERMINAL_COVERAGE_REPORTS = frozenset(
    {"term", "term-missing", "term:skip-covered", "term-missing:skip-covered"}
)


def _same_lexical_path(value: str, expected: Path) -> bool:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    candidate_key = os.path.normcase(
        os.path.abspath(os.path.normpath(os.fspath(candidate)))
    )
    expected_key = os.path.normcase(
        os.path.abspath(os.path.normpath(os.fspath(expected)))
    )
    return candidate_key == expected_key


def _validate_pytest_target(value: str) -> None:
    """Restrict collection to no-follow paths under the repository tests tree."""

    if value.startswith("@"):
        raise SystemExit("pytest response files are not allowed by the offline entrypoint")
    path_value = value.split("::", 1)[0]
    if not path_value:
        raise SystemExit("pytest collection target is empty")
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    root_lexical = Path(
        os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT)))
    )
    candidate_lexical = Path(
        os.path.abspath(os.path.normpath(os.fspath(candidate)))
    )
    tests_root = root_lexical / "tests"
    if not _is_relative_to(candidate_lexical, tests_root):
        raise SystemExit("pytest collection targets must remain under repository tests")
    relative = candidate_lexical.relative_to(root_lexical)
    _lstat_direct_candidate_chain(
        root_lexical=root_lexical,
        relative=relative,
        allow_missing=True,
    )


def _validate_pytest_arguments(arguments: list[str]) -> None:
    """Fail closed before importing pytest or any third-party plugin."""

    expected_basetemp = Path(os.environ["TMP_DIR"]) / "pytest"
    expected_cache = Path(os.environ["TMP_DIR"]) / "pytest-cache"
    positional_only = False
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        lower = argument.lower()
        if argument == "--":
            positional_only = True
            index += 1
            continue

        if not positional_only and argument.startswith("-"):
            if (
                lower in _PYTEST_UNSAFE_EXACT_OPTIONS
                or lower.startswith(_PYTEST_UNSAFE_LONG_PREFIXES)
                or re.fullmatch(r"-(?:c|o|p|n)(?:=|.+)", lower)
            ):
                # The wrapper's own trusted cache override is the only -o form.
                if lower == "-o" and index + 1 < len(arguments):
                    override = arguments[index + 1]
                    prefix = "cache_dir="
                    if override.lower().startswith(prefix) and _same_lexical_path(
                        override[len(prefix) :],
                        expected_cache,
                    ):
                        index += 2
                        continue
                raise SystemExit(
                    f"pytest option is not allowed by the offline entrypoint: {argument}"
                )

            if lower == "--basetemp":
                if index + 1 >= len(arguments) or not _same_lexical_path(
                    arguments[index + 1],
                    expected_basetemp,
                ):
                    raise SystemExit("pytest basetemp must use the selected DataRoot")
                index += 2
                continue
            if lower.startswith("--basetemp="):
                if not _same_lexical_path(
                    argument.split("=", 1)[1],
                    expected_basetemp,
                ):
                    raise SystemExit("pytest basetemp must use the selected DataRoot")
                index += 1
                continue

            if lower == "--cov":
                if index + 1 >= len(arguments):
                    raise SystemExit("pytest --cov requires a repository src module")
                source = arguments[index + 1]
                if not re.fullmatch(r"src(?:\.[A-Za-z_]\w*)*", source):
                    raise SystemExit("pytest coverage source must remain under src")
                index += 2
                continue
            if lower.startswith("--cov="):
                source = argument.split("=", 1)[1]
                if not re.fullmatch(r"src(?:\.[A-Za-z_]\w*)*", source):
                    raise SystemExit("pytest coverage source must remain under src")
                index += 1
                continue

            if lower == "--cov-report":
                if index + 1 >= len(arguments):
                    raise SystemExit("pytest --cov-report requires a terminal report")
                report = arguments[index + 1].lower()
                if report not in _PYTEST_TERMINAL_COVERAGE_REPORTS:
                    raise SystemExit("pytest coverage reports must remain terminal-only")
                index += 2
                continue
            if lower.startswith("--cov-report="):
                report = argument.split("=", 1)[1].lower()
                if report not in _PYTEST_TERMINAL_COVERAGE_REPORTS:
                    raise SystemExit("pytest coverage reports must remain terminal-only")
                index += 1
                continue

            if lower in _PYTEST_VALUE_OPTIONS:
                if index + 1 >= len(arguments):
                    raise SystemExit(f"pytest option requires a value: {argument}")
                index += 2
                continue
            index += 1
            continue

        _validate_pytest_target(argument)
        index += 1


def _run_pytest() -> None:
    # Do not let parent-shell plugin injection run during pytest.main startup.
    os.environ.pop("PYTEST_PLUGINS", None)
    os.environ.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
    os.environ["PYTEST_ADDOPTS"] = "--strict-markers"
    _validate_pytest_arguments(sys.argv[1:])
    import pytest

    raise SystemExit(pytest.main(sys.argv[1:]))


def _run_python(arguments: list[str]) -> None:
    """Execute a repository ``python -m`` or ``python script.py`` in-process.

    A second interpreter would lose the process-local Config and network guards,
    so the requested target is deliberately executed through ``runpy``.
    """

    if not arguments:
        raise SystemExit(
            "usage: offline_entrypoint.py python {-m module|script.py} "
            "[arguments...]"
        )

    # The launcher itself lives under tests/, so always restore the canonical
    # repository root before either runpy mode can resolve top-level imports.
    _prefer_project_root_on_sys_path()
    mode = arguments[0]
    if mode == "-m":
        if len(arguments) < 2 or not arguments[1] or arguments[1].startswith("-"):
            raise SystemExit("direct Python -m requires a module name")
        module_name = _validated_direct_module(arguments[1])
        sys.argv = [module_name, *arguments[2:]]
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
        return

    if mode.startswith("-"):
        raise SystemExit(f"unsupported direct Python option: {mode}")

    script = _validated_direct_script(mode)
    sys.argv = [str(script), *arguments[1:]]
    runpy.run_path(str(script), run_name="__main__")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: offline_entrypoint.py {cli|mcp|pytest|python} [arguments...]"
        )

    target = sys.argv[1]
    if target not in {"cli", "mcp", "pytest", "python"}:
        raise SystemExit(f"unsupported test child target: {target}")

    _validate_child_environment()
    load_local = _live_mode_enabled()
    if target in {"pytest", "python"} and load_local:
        raise RuntimeError("generic Direct Python/pytest is available only in offline mode")
    scrub_child_process_env(os.environ)
    if not load_local:
        for key in tuple(os.environ):
            if key.upper() in LIVE_ENV_KEYS:
                os.environ.pop(key, None)
        os.environ.update(
            {
                "PKV_RUN_LIVE": "0",
                OFFLINE_SENTINEL: "1",
                LOAD_LOCAL_SENTINEL: "0",
            }
        )
        install_offline_network_guard(block_raw_sockets=False)
        if target == "python":
            # ``platform`` may invoke the Windows ``ver`` command on first use.
            # Warm that standard-library cache before target process creation is
            # blocked so later dependency imports remain deterministic.
            platform.uname()
            install_offline_process_guard()

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    config_factory = _install_test_config(load_local=load_local)
    # pytest must retain the real Config class: configuration tests explicitly
    # construct alternate base/local pairs.  Parent plugin injection and
    # explicit pre-collection plugins are rejected; installed autoload plugins
    # remain part of the trusted Conda environment boundary.  The validated
    # singleton is already installed before pytest itself is imported.
    if target != "pytest":
        _bind_test_config_factory(config_factory)
    if not load_local:
        mark_offline_runtime_ready(process_guarded=target == "python")
    if target == "cli":
        _run_cli(config_factory)
        return
    if target == "mcp":
        _run_mcp()
        return
    if target == "pytest":
        _run_pytest()
        return
    _run_python(sys.argv[1:])


if __name__ == "__main__":
    main()
