"""Import-time probe for the generic Direct Python offline entrypoint."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile

from src.utils import config as config_module
from tests import offline_runtime


_PATHS = {
    "DATA_DIR": "data_dir",
    "DB_PATH": "db_path",
    "VAULT_DIR": "vault_dir",
    "VECTOR_DIR": "vector_index_dir",
    "LOG_DIR": "log_dir",
    "TMP_DIR": "tmp_dir",
}

_EARLY_BOOTSTRAP_ENV_KEYS = (
    "COVERAGE_PROCESS_START",
    "COVERAGE_PROCESS_CONFIG",
    "COVERAGE_RCFILE",
    "COVERAGE_FORCE_CONFIG",
    "COVERAGE_DEBUG",
    "COVERAGE_DEBUG_FILE",
    "COV_CORE_SOURCE",
    "COV_CORE_CONFIG",
    "COV_CORE_DATAFILE",
    "COV_CORE_BRANCH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONWARNINGS",
    "PYTHONUSERBASE",
)


def _assert_offline_bootstrap() -> dict[str, str]:
    direct_config = config_module.Config()
    shared_config = config_module.get_config()
    assert direct_config is shared_config
    assert direct_config._local_config_path is None

    resolved_paths: dict[str, str] = {}
    for env_key, attribute in _PATHS.items():
        actual = Path(getattr(direct_config, attribute)).resolve()
        expected = Path(os.environ[env_key]).resolve()
        assert actual == expected
        resolved_paths[env_key] = str(actual)

    for key in (
        "OPENAI_API_KEY",
        "PKV_LLM_API_KEY",
        "PKV_MCP_AUTH_TOKEN",
        "HTTPS_PROXY",
        "http_proxy",
        "PKV_E2E_ARCHIVE_URL",
    ):
        assert key not in os.environ
    assert os.environ["PKV_RUN_LIVE"] == "0"
    assert os.environ["PKV_TEST_OFFLINE"] == "1"
    assert os.environ["PKV_TEST_LOAD_LOCAL"] == "0"
    assert Path(os.environ["PKV_DATA_ROOT"]).resolve() == Path(
        os.environ["DATA_DIR"]
    ).resolve()
    assert os.environ["PYTHONNOUSERSITE"] == "1"
    assert Path(os.environ["PYTHONPATH"]).resolve() == Path(
        os.environ["PKV_TEST_PROJECT_ROOT"]
    ).resolve()
    for key in _EARLY_BOOTSTRAP_ENV_KEYS:
        assert key not in os.environ
    expected_tmp = Path(os.environ["TMP_DIR"]).resolve()
    for key in ("TEMP", "TMP", "TMPDIR"):
        assert Path(os.environ[key]).resolve() == expected_tmp
    assert Path(tempfile.gettempdir()).resolve() == expected_tmp
    expected_profile = Path(os.environ["USERPROFILE"]).resolve()
    assert Path(os.environ["HOME"]).resolve() == expected_profile
    assert Path.home().resolve() == expected_profile
    for key in (
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        assert Path(os.environ[key]).resolve().is_relative_to(expected_profile)

    assert socket.getaddrinfo is offline_runtime._blocked_network_call
    assert subprocess.Popen.__init__ is offline_runtime._blocked_process_call
    try:
        socket.getaddrinfo("network-sentinel.invalid", 443)
    except offline_runtime.OfflineNetworkError:
        pass
    else:  # pragma: no cover - a failed guard must fail the child immediately
        raise AssertionError("offline DNS guard was not installed")
    return resolved_paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-env", action="store_true")
    parser.add_argument("--exit-code", type=int, default=0)
    args, _unknown = parser.parse_known_args()

    paths = _assert_offline_bootstrap()
    if args.print_env:
        payload = {
            **paths,
            "COVERAGE_FILE": os.environ["COVERAGE_FILE"],
            "PKV_RUN_LIVE": os.environ["PKV_RUN_LIVE"],
            "PKV_DATA_ROOT": os.environ["PKV_DATA_ROOT"],
            "PKV_TEST_LOAD_LOCAL": os.environ["PKV_TEST_LOAD_LOCAL"],
            "PKV_TEST_OFFLINE": os.environ["PKV_TEST_OFFLINE"],
            "PKV_TEST_PROJECT_ROOT": os.environ["PKV_TEST_PROJECT_ROOT"],
            "PYTHONDONTWRITEBYTECODE": os.environ["PYTHONDONTWRITEBYTECODE"],
            "PYTEST_ADDOPTS": os.environ["PYTEST_ADDOPTS"],
            "HOME": os.environ["HOME"],
            "USERPROFILE": os.environ["USERPROFILE"],
            "APPDATA": os.environ["APPDATA"],
            "LOCALAPPDATA": os.environ["LOCALAPPDATA"],
            "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
            "XDG_CACHE_HOME": os.environ["XDG_CACHE_HOME"],
            "XDG_DATA_HOME": os.environ["XDG_DATA_HOME"],
            "TEMP": os.environ["TEMP"],
            "TMP": os.environ["TMP"],
            "TMPDIR": os.environ["TMPDIR"],
        }
        print("PKV_ENV_JSON=" + json.dumps(payload, sort_keys=True))
    return args.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
