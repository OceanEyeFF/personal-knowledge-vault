"""Fail-closed runtime helpers for automated tests.

This module intentionally depends on the standard library only.  Child-process
tests import it before importing any product entrypoint so inherited credentials,
proxy settings, and live-test switches cannot affect the default test path.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Callable, Mapping, MutableMapping


OFFLINE_SENTINEL = "PKV_TEST_OFFLINE"
LOAD_LOCAL_SENTINEL = "PKV_TEST_LOAD_LOCAL"
PROJECT_ROOT_SENTINEL = "PKV_TEST_PROJECT_ROOT"

RUNTIME_PATH_ENV_KEYS = frozenset(
    {
        "DATA_DIR",
        "DB_PATH",
        "LOG_DIR",
        "TMP_DIR",
        "VAULT_DIR",
        "VECTOR_DIR",
    }
)
SAFE_RUNTIME_OVERRIDE_KEYS = RUNTIME_PATH_ENV_KEYS | {"LOG_LEVEL"}

PROXY_ENV_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)

EXPLICIT_SECRET_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "COHERE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "HUGGINGFACEHUB_API_TOKEN",
        "HF_TOKEN",
        "OPENAI_API_KEY",
        "PKV_EMBD_API_KEY",
        "PKV_LLM_API_KEY",
        "PKV_MCP_AUTH_TOKEN",
    }
)

PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AZURE_OPENAI_",
    "COHERE_",
    "DEEPSEEK_",
    "GEMINI_",
    "GOOGLE_",
    "HUGGINGFACE_",
    "OPENAI_",
    "PKV_EMBD_",
    "PKV_LLM_",
)

LIVE_ENV_KEYS = frozenset(
    {
        "PKV_E2E_ARCHIVE_URL",
        "PKV_RUN_LIVE",
        LOAD_LOCAL_SENTINEL,
    }
)

_SECRET_NAME_PARTS = frozenset(
    {
        "AUTH",
        "COOKIE",
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "PASSWORD",
        "PASSWD",
        "SECRET",
        "TOKEN",
    }
)


class OfflineNetworkError(RuntimeError):
    """Raised when an automated offline test attempts network I/O."""


def _normalise_env_name(name: str) -> str:
    return name.upper()


def _is_secret_or_provider_env(name: str) -> bool:
    upper_name = _normalise_env_name(name)
    if upper_name in EXPLICIT_SECRET_ENV_KEYS:
        return True
    if upper_name.startswith(PROVIDER_ENV_PREFIXES):
        return True
    return bool(_SECRET_NAME_PARTS.intersection(upper_name.split("_")))


def _remove_case_insensitive(
    env: MutableMapping[str, str],
    names: frozenset[str],
) -> None:
    for key in tuple(env):
        if _normalise_env_name(key) in names:
            env.pop(key, None)


def _remove_secrets_and_providers(env: MutableMapping[str, str]) -> None:
    for key in tuple(env):
        if _is_secret_or_provider_env(key):
            env.pop(key, None)


def _validate_runtime_overrides(
    runtime_overrides: Mapping[str, str | os.PathLike[str]],
) -> None:
    """Prevent callers from reintroducing live, proxy, or secret state."""

    for key in runtime_overrides:
        upper_key = _normalise_env_name(key)
        if (
            upper_key in PROXY_ENV_KEYS | LIVE_ENV_KEYS
            or _is_secret_or_provider_env(key)
        ):
            raise ValueError(f"unsafe child runtime override: {key}")
        if upper_key not in SAFE_RUNTIME_OVERRIDE_KEYS:
            raise ValueError(f"unsupported child runtime override: {key}")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _lexical_path(
    value: str | os.PathLike[str],
    project_root: Path | None = None,
) -> Path:
    """Normalize a path without touching the filesystem or a UNC target."""

    path = Path(value)
    if not path.is_absolute() and project_root is not None:
        path = project_root / path
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _is_lexically_relative_to(path: Path, parent: Path) -> bool:
    path_key = os.path.normcase(os.fspath(path))
    parent_key = os.path.normcase(os.fspath(parent))
    try:
        common = os.path.commonpath((path_key, parent_key))
    except ValueError:
        return False
    return common == parent_key


def validate_test_runtime_paths(
    *,
    project_root: Path,
    runtime_overrides: Mapping[str, str | os.PathLike[str]],
) -> dict[str, str]:
    """Contain paths lexically before canonicalizing anything under ``.data-test``."""

    _validate_runtime_overrides(runtime_overrides)
    normalized: dict[str, str | os.PathLike[str]] = {}
    for key, value in runtime_overrides.items():
        upper_key = _normalise_env_name(key)
        if upper_key in normalized:
            raise ValueError(f"duplicate child runtime override: {key}")
        normalized[upper_key] = value

    if "DATA_DIR" not in normalized:
        raise ValueError("child runtime override requires DATA_DIR")

    root_lexical = _lexical_path(project_root)
    allowed_lexical = root_lexical / ".data-test"
    lexical_paths: dict[str, Path] = {}
    for key, value in normalized.items():
        if key not in RUNTIME_PATH_ENV_KEYS:
            continue
        lexical = _lexical_path(value, root_lexical)
        lexical_paths[key] = lexical

    data_lexical = lexical_paths["DATA_DIR"]
    if not _is_lexically_relative_to(data_lexical, allowed_lexical):
        raise ValueError(f"unsafe automated-test DATA_DIR: {data_lexical}")
    for key, lexical in lexical_paths.items():
        if key != "DATA_DIR" and not _is_lexically_relative_to(
            lexical,
            data_lexical,
        ):
            raise ValueError(f"{key} must remain inside automated-test DATA_DIR")

    # Only lexically accepted paths may touch the filesystem for reparse-point
    # canonicalization. This prevents rejected .data/UNC candidates being probed.
    root = root_lexical.resolve()
    expected_test_root = root / ".data-test"
    allowed_test_root = expected_test_root.resolve()
    if (
        allowed_test_root != expected_test_root
        or allowed_test_root == root
        or not _is_relative_to(allowed_test_root, root)
    ):
        raise ValueError(f"unsafe automated-test root: {allowed_test_root}")
    data_dir = data_lexical.resolve()
    if not _is_relative_to(data_dir, allowed_test_root):
        raise ValueError(f"unsafe automated-test DATA_DIR: {data_dir}")

    canonical: dict[str, str] = {}
    for key, value in normalized.items():
        if key in RUNTIME_PATH_ENV_KEYS:
            resolved = lexical_paths[key].resolve()
            if key != "DATA_DIR" and not _is_relative_to(resolved, data_dir):
                raise ValueError(
                    f"{key} must remain inside automated-test DATA_DIR"
                )
            canonical[key] = str(resolved)
        else:
            canonical[key] = str(value)
    return canonical


_CONFIG_PATH_ATTRIBUTES = {
    "DATA_DIR": "data_dir",
    "DB_PATH": "db_path",
    "VAULT_DIR": "vault_dir",
    "VECTOR_DIR": "vector_index_dir",
    "LOG_DIR": "log_dir",
    "TMP_DIR": "tmp_dir",
}


def assert_config_runtime_paths(
    config: object,
    expected_paths: Mapping[str, str | os.PathLike[str]],
) -> None:
    """Fail before ``ensure_dirs`` if Config ignores a validated override."""

    for env_key, attribute in _CONFIG_PATH_ATTRIBUTES.items():
        if env_key not in expected_paths:
            raise RuntimeError(f"validated runtime is missing {env_key}")
        actual_lexical = _lexical_path(getattr(config, attribute))
        expected_lexical = _lexical_path(expected_paths[env_key])
        if os.path.normcase(os.fspath(actual_lexical)) != os.path.normcase(
            os.fspath(expected_lexical)
        ):
            raise RuntimeError(
                f"Config {attribute} escaped validated runtime: "
                f"{actual_lexical} != {expected_lexical}"
            )
        # Lexical equality means this canonicalization only touches a path that
        # already passed the .data-test validator.
        if actual_lexical.resolve() != expected_lexical.resolve():
            raise RuntimeError(f"Config {attribute} canonical path drifted")


def scrub_child_process_env(env: MutableMapping[str, str]) -> None:
    """Remove inherited credentials and proxy values before product imports."""

    _remove_case_insensitive(env, PROXY_ENV_KEYS)
    _remove_secrets_and_providers(env)


def prepare_offline_child_env(
    *,
    project_root: Path,
    runtime_overrides: Mapping[str, str | os.PathLike[str]],
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a deterministic, base-config-only environment for a child.

    ``parent_env`` is copied and never mutated.  Runtime path overrides are
    applied only after all live, secret/provider, and proxy values are removed.
    """

    root = project_root.resolve()
    env = dict(os.environ if parent_env is None else parent_env)
    safe_overrides = validate_test_runtime_paths(
        project_root=root,
        runtime_overrides=runtime_overrides,
    )
    _remove_case_insensitive(env, PROXY_ENV_KEYS | LIVE_ENV_KEYS)
    _remove_secrets_and_providers(env)

    env.update(safe_overrides)
    env.update(
        {
            "PKV_RUN_LIVE": "0",
            OFFLINE_SENTINEL: "1",
            LOAD_LOCAL_SENTINEL: "0",
            PROJECT_ROOT_SENTINEL: str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(root),
        }
    )
    return env


def prepare_live_child_env(
    *,
    project_root: Path,
    runtime_overrides: Mapping[str, str | os.PathLike[str]],
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an isolated env that explicitly opts into local live config.

    This helper is intentionally strict: callers cannot accidentally construct a
    live child unless the parent process was itself launched with
    ``PKV_RUN_LIVE=1``.  Provider/auth secrets from the shell are still removed;
    the selected live fixture must load ``config/local.yaml`` explicitly.
    """

    root = project_root.resolve()
    source_env = dict(os.environ if parent_env is None else parent_env)
    safe_overrides = validate_test_runtime_paths(
        project_root=root,
        runtime_overrides=runtime_overrides,
    )
    if source_env.get("PKV_RUN_LIVE") != "1":
        raise RuntimeError("live child requires explicit PKV_RUN_LIVE=1")

    env = dict(source_env)
    scrub_child_process_env(env)
    env.update(safe_overrides)
    env.update(
        {
            "PKV_RUN_LIVE": "1",
            OFFLINE_SENTINEL: "0",
            LOAD_LOCAL_SENTINEL: "1",
            PROJECT_ROOT_SENTINEL: str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(root),
        }
    )
    return env


def _blocked_network_call(*_args, **_kwargs):
    raise OfflineNetworkError(
        "outbound network is disabled for automated offline tests"
    )


_ORIGINAL_RAW_SOCKET_METHODS = {
    name: getattr(socket.socket, name)
    for name in ("connect", "connect_ex", "sendto")
}


def _is_internal_socket_destination(address: object) -> bool:
    """Allow only local socketpair/Unix-socket destinations."""

    if isinstance(address, (str, bytes)):
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def install_offline_network_guard(
    patch: Callable[[object, str, object], None] | None = None,
    *,
    block_raw_sockets: bool = True,
) -> None:
    """Block DNS, stream connects, and datagram sends in the current process.

    ``patch`` may be ``monkeypatch.setattr`` so pytest can restore the original
    functions after a test. With no patch callback the guard is process-wide.
    When ``block_raw_sockets`` is false, raw sockets are still guarded but
    loopback/AF_UNIX destinations remain available for asyncio's Windows
    socketpair implementation. This is not an OS-level sandbox.
    """

    def set_attr(target: object, name: str, value: object) -> None:
        if patch is None:
            setattr(target, name, value)
        else:
            patch(target, name, value)

    for name in (
        "create_connection",
        "getaddrinfo",
        "gethostbyaddr",
        "gethostbyname",
        "gethostbyname_ex",
        "getnameinfo",
    ):
        set_attr(socket, name, _blocked_network_call)

    for name, original in _ORIGINAL_RAW_SOCKET_METHODS.items():
        if block_raw_sockets:
            set_attr(socket.socket, name, _blocked_network_call)
            continue

        def guard_internal_only(
            sock: socket.socket,
            *args,
            _original=original,
            **kwargs,
        ):
            address = args[-1] if args else kwargs.get("address")
            if not _is_internal_socket_destination(address):
                return _blocked_network_call()
            return _original(sock, *args, **kwargs)

        set_attr(socket.socket, name, guard_internal_only)

    if hasattr(socket.socket, "sendmsg"):
        set_attr(socket.socket, "sendmsg", _blocked_network_call)
