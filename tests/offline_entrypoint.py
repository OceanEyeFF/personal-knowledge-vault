"""Dedicated base-config entrypoint for offline child-process tests."""

from __future__ import annotations

import os
import platform
import re
import runpy
import json
import stat
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

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
_SYNTHETIC_READY_RUNTIME_SENTINEL = "PKV_TEST_SYNTHETIC_RUNTIME_READY"
_ABSENT_DATA_ROOT_SENTINEL = "PKV_TEST_ABSENT_DATA_ROOT"
_TEST_USER_CONFIG_SENTINEL = "PKV_TEST_USER_CONFIG"
_MAX_TEST_USER_CONFIG_BYTES = 64 * 1024
_BLACKBOX_API_KEY = "pkv-blackbox-not-a-secret"
_BLACKBOX_LLM_MODEL = "pkv-r4-blackbox-chat-v1"
_BLACKBOX_EMBEDDING_MODEL = "pkv-r4-blackbox-embedding-v1"


def _live_mode_enabled() -> bool:
    load_local = os.environ.get(LOAD_LOCAL_SENTINEL) == "1"
    run_live = os.environ.get("PKV_RUN_LIVE") == "1"
    offline = os.environ.get(OFFLINE_SENTINEL) == "1"
    if load_local and run_live and not offline:
        return True
    if not load_local and not run_live and offline:
        return False
    raise RuntimeError("child environment live/offline sentinels are inconsistent")


def _live_user_config_path() -> Path:
    """Return the user-owned config source allowed for an opted-in live child."""

    user_home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    profile_root = Path(user_home) if user_home else Path.home()
    return profile_root / ".pkv" / "config.yaml"


def _validate_child_environment() -> None:
    expected_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT))))
    configured_root = os.environ.get(PROJECT_ROOT_SENTINEL)
    if configured_root is None or not Path(configured_root).is_absolute():
        raise RuntimeError("child project-root sentinel is missing or invalid")
    configured_lexical = Path(os.path.abspath(os.path.normpath(configured_root)))
    if os.path.normcase(os.fspath(configured_lexical)) != os.path.normcase(
        os.fspath(expected_lexical)
    ):
        raise RuntimeError("child project-root sentinel is missing or invalid")

    expected_root = expected_lexical.resolve()
    if configured_lexical.resolve() != expected_root:
        raise RuntimeError("child project-root sentinel is missing or invalid")

    runtime_overrides = {
        key: os.environ[key] for key in RUNTIME_PATH_ENV_KEYS if key in os.environ
    }
    canonical = validate_test_runtime_paths(
        project_root=expected_root,
        runtime_overrides=runtime_overrides,
    )
    os.environ.update(canonical)
    # This formal product override has higher precedence than legacy DATA_DIR.
    # Pin it to the already validated child root before importing Config so an
    # equivalent direct/CI launcher cannot leak an inherited user data root.
    os.environ["PKV_DATA_ROOT"] = canonical["DATA_DIR"]


def _install_test_config(
    *,
    load_local: bool,
    leave_data_root_absent: bool = False,
    test_user_config_path: str | None = None,
) -> Callable[..., object]:
    """Install Config before importing either product entrypoint."""

    from src.utils import config as config_module

    synthetic_ready_runtime = os.environ.get(_SYNTHETIC_READY_RUNTIME_SENTINEL) == "1"
    if load_local and test_user_config_path is not None:
        raise RuntimeError("live child cannot use an offline test user config")
    user_config_path = (
        str(_live_user_config_path()) if load_local else test_user_config_path
    )

    def new_config(*_args, **_kwargs):
        if not synthetic_ready_runtime or test_user_config_path is not None:
            return config_module.Config(
                str(BASE_CONFIG_PATH),
                user_config_path=user_config_path,
            )
        # This is an explicit child-fixture seam, never a product configuration
        # source.  The placeholder values only make structural lifecycle
        # validation deterministic; offline guards still prohibit Provider I/O.
        return config_module.Config(
            str(BASE_CONFIG_PATH),
            user_config_path=user_config_path,
            _user_config_updates={
                "ai.llm.api_key": "offline-test-placeholder",
                "ai.embedding.api_key": "offline-test-placeholder",
            },
        )

    config = new_config()
    assert_config_runtime_paths(
        config,
        {key: os.environ[key] for key in RUNTIME_PATH_ENV_KEYS},
    )
    if synthetic_ready_runtime and leave_data_root_absent:
        raise RuntimeError(
            "an absent-data-root child cannot also request a synthetic READY runtime"
        )
    if leave_data_root_absent:
        # This is deliberately a test-entrypoint-only seam for L3 status-only
        # evidence.  A normal offline child still uses ``ensure_dirs`` below;
        # callers opting in here must prove the selected root did not already
        # exist before any product entrypoint is imported.
        if os.path.lexists(config.layout.user_data_root):
            raise RuntimeError("absent-data-root child requires a nonexistent DATA_DIR")
    elif synthetic_ready_runtime:
        # The child-only READY seam owns the one setup write it may need.  Do
        # not call ``ensure_dirs`` first: it takes the product writer lease,
        # which would make every fresh read-only subprocess look like a writer.
        _seed_synthetic_ready_runtime_snapshot(config)
    else:
        config.ensure_dirs()
    config_module._config_instance = config

    def validated_config_factory(*_args, **_kwargs):
        return config

    return validated_config_factory


def _consume_absent_data_root_request() -> bool:
    """Read the narrowly scoped L3 absent-root harness switch once.

    It is intentionally removed from the child environment before product code
    starts, so it cannot become a de-facto runtime configuration input.
    """

    value = os.environ.pop(_ABSENT_DATA_ROOT_SENTINEL, None)
    if value is None:
        return False
    if value != "1":
        raise RuntimeError("invalid absent-data-root child request")
    return True


def _strict_json_object(raw: bytes) -> dict[str, object]:
    def reject_duplicates(pairs):
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise RuntimeError("test user config contains duplicate keys")
            value[key] = item
        return value

    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("test user config must be strict UTF-8 JSON/YAML") from exc
    if type(parsed) is not dict:
        raise RuntimeError("test user config root is invalid")
    return parsed


def _test_provider_endpoint(value: object) -> tuple[str, int]:
    if type(value) is not str:
        raise RuntimeError("test Provider base_url is invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("test Provider base_url port is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("test Provider must use an exact numeric-loopback URL")
    return "127.0.0.1", port


def _validate_test_user_config(raw: bytes) -> frozenset[tuple[str, int]]:
    payload = _strict_json_object(raw)
    if set(payload) != {"ai"} or type(payload["ai"]) is not dict:
        raise RuntimeError("test user config fields are invalid")
    ai = payload["ai"]
    if set(ai) != {"llm", "embedding", "automation"}:
        raise RuntimeError("test user AI config fields are invalid")
    llm = ai["llm"]
    embedding = ai["embedding"]
    automation = ai["automation"]
    if type(llm) is not dict or set(llm) != {
        "provider",
        "api_key",
        "base_url",
        "model",
        "timeout_seconds",
        "max_retries",
    }:
        raise RuntimeError("test LLM config fields are invalid")
    if type(embedding) is not dict or set(embedding) != {
        "provider",
        "api_key",
        "base_url",
        "model",
        "dim",
        "timeout_seconds",
        "max_retries",
    }:
        raise RuntimeError("test Embedding config fields are invalid")
    if any(
        type(provider["provider"]) is not str
        or provider["provider"] != "openai_compatible"
        or type(provider["api_key"]) is not str
        or provider["api_key"] != _BLACKBOX_API_KEY
        or type(provider["max_retries"]) is not int
        or provider["max_retries"] != 0
        or type(provider["timeout_seconds"]) is not int
        or not 1 <= provider["timeout_seconds"] <= 30
        for provider in (llm, embedding)
    ):
        raise RuntimeError("test Provider settings are invalid")
    if llm["model"] != _BLACKBOX_LLM_MODEL:
        raise RuntimeError("test LLM model is invalid")
    if embedding["model"] != _BLACKBOX_EMBEDDING_MODEL or embedding["dim"] != 3:
        raise RuntimeError("test Embedding contract is invalid")
    llm_endpoint = _test_provider_endpoint(llm["base_url"])
    embedding_endpoint = _test_provider_endpoint(embedding["base_url"])
    if llm_endpoint != embedding_endpoint:
        raise RuntimeError("test Provider endpoints must use one harness")

    if type(automation) is not dict or set(automation) != {
        "schema_version",
        "enabled",
        "authorization",
        "token_budget",
        "retry",
    }:
        raise RuntimeError("test automation config fields are invalid")
    authorization = automation["authorization"]
    budget = automation["token_budget"]
    retry = automation["retry"]
    policy_sha256 = (
        authorization.get("policy_sha256")
        if type(authorization) is dict and set(authorization) == {"policy_sha256"}
        else None
    )
    if (
        type(automation["schema_version"]) is not int
        or automation["schema_version"] != 1
        or automation["enabled"] is not True
        or type(policy_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", policy_sha256) is None
        or type(budget) is not dict
        or set(budget) != {"timezone", "daily_total_tokens", "monthly_total_tokens"}
        or budget["timezone"] != "UTC"
        or type(budget["daily_total_tokens"]) is not int
        or type(budget["monthly_total_tokens"]) is not int
        or not 1_000 <= budget["daily_total_tokens"] <= 10_000_000
        or not budget["daily_total_tokens"]
        <= budget["monthly_total_tokens"]
        <= 100_000_000
        or type(retry) is not dict
        or set(retry) != {"max_attempts"}
        or type(retry["max_attempts"]) is not int
        or retry["max_attempts"] != 2
    ):
        raise RuntimeError("test automation policy is invalid")
    return frozenset({llm_endpoint})


def _consume_test_user_config_request(
    *,
    target: str,
    load_local: bool,
) -> tuple[str, frozenset[tuple[str, int]]] | None:
    """Validate one offline, DataRoot-contained synthetic user YAML source.

    The path is consumed by this dedicated test entrypoint and removed before
    product code starts.  The product still receives the normal public
    ``Config(..., user_config_path=...)`` interface; no Provider setting is read
    from an environment variable or injected into a product module.
    """

    raw = os.environ.pop(_TEST_USER_CONFIG_SENTINEL, None)
    if raw is None:
        return None
    if os.environ.get(_SYNTHETIC_READY_RUNTIME_SENTINEL) != "1":
        raise RuntimeError(
            "test user config requires an explicitly synthetic ready runtime"
        )
    if load_local or target not in {"cli", "mcp"}:
        raise RuntimeError(
            "test user config is available only to offline CLI/MCP children"
        )
    if not raw or "\x00" in raw:
        raise RuntimeError("test user config path is invalid")

    data_root = Path(os.environ["DATA_DIR"])
    expected = data_root / "profile" / ".pkv" / "config.yaml"
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RuntimeError("test user config path must be absolute")
    candidate_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
    expected_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(expected))))
    if os.path.normcase(os.fspath(candidate_lexical)) != os.path.normcase(
        os.fspath(expected_lexical)
    ):
        raise RuntimeError("test user config must use the selected DataRoot profile")

    cursor = data_root
    relative = expected_lexical.relative_to(data_root)
    item_stat: os.stat_result | None = None
    for index, part in enumerate(relative.parts):
        cursor /= part
        try:
            item_stat = os.lstat(cursor)
        except OSError as exc:
            raise RuntimeError("test user config is unavailable") from exc
        file_attributes = getattr(item_stat, "st_file_attributes", 0)
        if stat.S_ISLNK(item_stat.st_mode) or bool(file_attributes & 0x400):
            raise RuntimeError("test user config path contains a link")
        is_leaf = index == len(relative.parts) - 1
        if not is_leaf and not stat.S_ISDIR(item_stat.st_mode):
            raise RuntimeError("test user config parent is invalid")
    if (
        item_stat is None
        or not stat.S_ISREG(item_stat.st_mode)
        or item_stat.st_nlink != 1
        or not 1 <= item_stat.st_size <= _MAX_TEST_USER_CONFIG_BYTES
    ):
        raise RuntimeError("test user config file is invalid")
    if candidate_lexical.resolve(strict=True) != expected_lexical.resolve(strict=True):
        raise RuntimeError("test user config path canonicalization drifted")
    endpoints = _validate_test_user_config(candidate_lexical.read_bytes())
    return str(expected_lexical), endpoints


def _seed_synthetic_ready_runtime_snapshot(config: object) -> None:
    """Publish a secret-free snapshot for an explicitly seeded offline fixture.

    The helper neither initializes nor migrates a database.  It only recognizes
    an already-initialized isolated SQLite fixture and, when the snapshot is
    absent, writes the same small runtime record that a confirmed lifecycle
    action would publish.  It never refreshes, repairs, or overwrites an
    existing snapshot: a new child must observe degradation/drift left by an
    earlier operation exactly as production would.  A fresh root stays unready,
    which lets lifecycle/status-only tests exercise the product path without a
    test-specific bypass.
    """

    from src.runtime.errors import PKVRuntimeError
    from src.storage.migration_manager import DatabaseState, MigrationManager

    layout = config.layout
    database = MigrationManager(
        layout.db_path,
        layout.migrations_dir,
        read_only=True,
        backup_dir=layout.backup_dir,
    ).inspect_database()
    if database.state is not DatabaseState.READY:
        return

    reader = getattr(config, "read_runtime_config_snapshot", None)
    if callable(reader):
        try:
            if reader() is not None:
                return
        except (OSError, TypeError, ValueError, PKVRuntimeError):
            # A malformed or unsafe fixture snapshot is intentional state for
            # the lifecycle gate to report.  Never hide it by publishing a
            # synthetic replacement from a child-process test seam.
            return

    dimension = config.embedding_dim
    if type(dimension) is not int or dimension < 1:
        raise RuntimeError(
            "synthetic ready fixture requires a declared embedding dimension"
        )

    # This branch is reached only when this child is the one that publishes the
    # absent synthetic runtime snapshot.  Keep the cache prewarm in that same
    # root writer scope: a later read-only child observes the durable fixture
    # state without acquiring a lease, while a pre-existing snapshot/cache is
    # never refreshed or silently repaired by the harness.
    from src.runtime.write_lease import write_lease_scope
    from src.utils.text_utils import TextProcessor

    with write_lease_scope(layout):
        config.write_runtime_config_snapshot(
            {
                "schema_version": 1,
                "database": {"schema_version": database.current_version},
                "embedding": {
                    "provider": config.embd_provider,
                    "fingerprint": config.embedding_index_fingerprint(dimension),
                },
            }
        )
        TextProcessor(runtime_config=config, initialize_cache=True)


def _bind_test_config_factory(config_factory: Callable[..., object]) -> None:
    """Make later direct ``Config()`` imports return the validated singleton."""

    from src.utils import config as config_module

    original_config_class = config_module.Config

    class _ValidatedConfigMeta(type(original_config_class)):
        def __call__(cls, *args, **kwargs):
            # ``Config()`` is the legacy process-default seam. Explicit
            # constructor arguments are used by reload/configuration code to
            # build a candidate immutable snapshot and must retain their real
            # semantics instead of silently returning the current singleton.
            if args or kwargs:
                return original_config_class(*args, **kwargs)
            return config_factory()

        def __instancecheck__(cls, instance: object) -> bool:
            return isinstance(instance, original_config_class)

    class ValidatedConfig(original_config_class, metaclass=_ValidatedConfigMeta):
        """Child-local constructor facade retaining Config's class API.

        Replacing ``Config`` with a bare function used to work for simple
        command tests, but it breaks class/static methods whose implementation
        resolves ``Config`` from the module namespace (for example runtime
        snapshot validation).  A narrow type facade keeps those APIs and
        ``isinstance`` checks intact while directing fresh construction to the
        already-validated, isolated singleton.
        """

    # This mutation is process-local. The dedicated child exits after the
    # requested target, so no production process observes the constructor
    # facade or its validated singleton.
    config_module.Config = ValidatedConfig


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


_PROHIBITED_DIRECT_ROOT_NAMES = frozenset({".data", ".data-test", ".git", "config"})


def _direct_candidate_lexical(value: str) -> tuple[Path, Path, Path]:
    """Validate a Direct Python path lexically without touching its target."""

    root_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT))))
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root_lexical / candidate
    candidate_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
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
        raise SystemExit(
            "direct Python script does not exist or is not readable"
        ) from exc
    root = root_lexical.resolve()
    if not _is_relative_to(resolved, root):
        raise SystemExit("direct Python script escaped the project root")
    resolved_relative = resolved.relative_to(root)
    if (
        not resolved_relative.parts
        or resolved_relative.parts[0].casefold() in _PROHIBITED_DIRECT_ROOT_NAMES
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
            raise SystemExit(
                "direct Python module is not an executable repository target"
            )

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
    f"{name}=" for name in _PYTEST_UNSAFE_EXACT_OPTIONS if name.startswith("--")
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
        raise SystemExit(
            "pytest response files are not allowed by the offline entrypoint"
        )
    path_value = value.split("::", 1)[0]
    if not path_value:
        raise SystemExit("pytest collection target is empty")
    candidate = Path(path_value)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    root_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(PROJECT_ROOT))))
    candidate_lexical = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
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
                    raise SystemExit(
                        "pytest coverage reports must remain terminal-only"
                    )
                index += 2
                continue
            if lower.startswith("--cov-report="):
                report = argument.split("=", 1)[1].lower()
                if report not in _PYTEST_TERMINAL_COVERAGE_REPORTS:
                    raise SystemExit(
                        "pytest coverage reports must remain terminal-only"
                    )
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
            "usage: offline_entrypoint.py python {-m module|script.py} [arguments...]"
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

    leave_data_root_absent = _consume_absent_data_root_request()
    _validate_child_environment()
    load_local = _live_mode_enabled()
    test_user_config_request = _consume_test_user_config_request(
        target=target,
        load_local=load_local,
    )
    test_user_config_path = (
        test_user_config_request[0] if test_user_config_request is not None else None
    )
    if leave_data_root_absent and (load_local or target not in {"cli", "mcp"}):
        raise RuntimeError(
            "absent-data-root child is available only for offline CLI/MCP tests"
        )
    if leave_data_root_absent and test_user_config_path is not None:
        raise RuntimeError("absent-data-root child cannot use a test user config")
    if target in {"pytest", "python"} and load_local:
        raise RuntimeError(
            "generic Direct Python/pytest is available only in offline mode"
        )
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
        install_offline_network_guard(
            block_raw_sockets=False,
            allowed_stream_endpoints=(
                test_user_config_request[1]
                if test_user_config_request is not None
                else None
            ),
        )
        if target == "python":
            # ``platform`` may invoke the Windows ``ver`` command on first use.
            # Warm that standard-library cache before target process creation is
            # blocked so later dependency imports remain deterministic.
            platform.uname()
            install_offline_process_guard()

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    config_factory = _install_test_config(
        load_local=load_local,
        leave_data_root_absent=leave_data_root_absent,
        test_user_config_path=test_user_config_path,
    )
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
