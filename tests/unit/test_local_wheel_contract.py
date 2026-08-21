"""K1b contracts for an offline local ``pkv-kernel`` wheel.

The test deliberately builds without dependency resolution, installs into a
new source-free venv in a repository-external temporary workspace, and launches
an isolated interpreter.  PKV runtime data remains below the isolated test root.
It is evidence of source-tree independence, not a release/PyPI workflow.
"""

from __future__ import annotations

from email.parser import Parser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]

WHEEL_RESOURCE_PREFIX = "pkv_kernel/_resources/"
EXPECTED_WHEEL_RESOURCE_PAYLOAD = frozenset(
    {
        "pkv_kernel/_resources/config/config.yaml",
        "pkv_kernel/_resources/config/custom_dict.txt",
        "pkv_kernel/_resources/config/workflows/archive-text.yaml",
        "pkv_kernel/_resources/config/workflows/archive-url.yaml",
        "pkv_kernel/_resources/scripts/migrations/001_initial_schema.sql",
        "pkv_kernel/_resources/scripts/migrations/002_add_cli_tables.sql",
        "pkv_kernel/_resources/scripts/migrations/004_add_chat_sessions.sql",
        "pkv_kernel/_resources/scripts/migrations/005_add_review_system.sql",
        "pkv_kernel/_resources/scripts/migrations/006_add_relations_foundation.sql",
        "pkv_kernel/_resources/scripts/migrations/007_add_timeline_time_fields.sql",
        "pkv_kernel/_resources/scripts/migrations/008_align_fts_contract.sql",
        "pkv_kernel/_resources/scripts/migrations/009_repair_fts_storage_contract.sql",
        "pkv_kernel/_resources/scripts/migrations/010_add_storage_operation_commits.sql",
        "pkv_kernel/_resources/src/ai/prompts/extract_tags.txt",
        "pkv_kernel/_resources/src/ai/prompts/summarize.txt",
    }
)
EXPECTED_REQUIRES_DIST = (
    "PyYAML==6.0.1",
    "beautifulsoup4==4.12.3",
    "hnswlib==0.8.0",
    "html2text==2020.1.16",
    "httpx<1,>=0.27",
    "jieba==0.42.1",
    "lxml==5.3.0",
    "numpy>=1.24",
    "openai<2,>=1.55.3",
    "python-frontmatter==1.1.0",
    "rich==13.7.0",
    "urllib3<3,>=2.2",
)


def _lexical_absolute(path: Path) -> Path:
    """Normalize a path without probing a real user profile on disk."""

    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed: {' '.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _clean_environment(
    data_root: Path,
    *,
    synthetic_home: Path,
    synthetic_temp: Path,
) -> dict[str, str]:
    """Build a child environment with no ambient PKV/profile/network settings.

    The wheel probe is deliberately run outside the source checkout.  Its home
    directory must be equally synthetic: otherwise a new ``Config()`` could
    silently read the operator's real ``%USERPROFILE%\\.pkv\\config.yaml``.
    """

    environment = dict(os.environ)
    for key in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TEMP",
        "TMP",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "PKV_DATA_ROOT",
        "PKV_LOG_LEVEL",
        "PKV_TEST_OFFLINE",
        "PKV_RUN_LIVE",
        "DATA_DIR",
        "DB_PATH",
        "VAULT_DIR",
        "VECTOR_DIR",
        "LOG_DIR",
        "TMP_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "PIP_CONFIG_FILE",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "PIP_FIND_LINKS",
        "PIP_TRUSTED_HOST",
    ):
        environment.pop(key, None)

    synthetic_home = synthetic_home.resolve()
    synthetic_temp = synthetic_temp.resolve()
    synthetic_data_root = data_root.resolve()
    local_app_data = synthetic_home / "AppData" / "Local"
    roaming_app_data = synthetic_home / "AppData" / "Roaming"
    for path in (
        synthetic_home,
        synthetic_temp,
        synthetic_data_root,
        local_app_data,
        roaming_app_data,
        synthetic_home / ".cache",
        synthetic_home / ".config",
    ):
        path.mkdir(parents=True, exist_ok=True)

    home_text = str(synthetic_home)
    home_drive = synthetic_home.drive
    environment["PKV_DATA_ROOT"] = str(data_root)
    environment.update(
        {
            "HOME": home_text,
            "USERPROFILE": home_text,
            "HOMEDRIVE": home_drive,
            "HOMEPATH": home_text[len(home_drive) :] if home_drive else home_text,
            "APPDATA": str(roaming_app_data),
            "LOCALAPPDATA": str(local_app_data),
            "TEMP": str(synthetic_temp),
            "TMP": str(synthetic_temp),
            "TMPDIR": str(synthetic_temp),
            "PYTHONUSERBASE": str(synthetic_home / "python-user-base"),
            "PYTHONNOUSERSITE": "1",
            "XDG_CACHE_HOME": str(synthetic_home / ".cache"),
            "XDG_CONFIG_HOME": str(synthetic_home / ".config"),
        }
    )
    return environment


def _venv_python(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.mark.packaging_contract
def test_local_wheel_clean_install_uses_only_installed_kernel_and_embedded_resources(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("K1b local-wheel contract currently supports Windows only")
    synthetic_data_root = tmp_path / "data"
    synthetic_home = tmp_path / "synthetic-home"
    synthetic_temp = tmp_path / "synthetic-temp"
    real_profile = _lexical_absolute(
        Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    )
    assert not synthetic_home.resolve().is_relative_to(real_profile)
    # The only checkout-external object in K1b is this disposable build/venv
    # workspace.  Its runtime root, profile, temp directory, and every probe
    # input remain under pytest's wrapper-selected ``.data-test`` root.  Do
    # not derive this directory from ``LOCALAPPDATA``: the wrapper correctly
    # makes that value a synthetic profile path inside ``.data-test``.
    workspace_parent = PROJECT_ROOT.parent.resolve()
    with tempfile.TemporaryDirectory(
        prefix="pkv-k1b-clean-install-", dir=workspace_parent
    ) as temporary:
        clean_workspace = Path(temporary)
        assert not clean_workspace.resolve().is_relative_to(PROJECT_ROOT.resolve())
        wheel_dir = clean_workspace / "wheelhouse"
        wheel_dir.mkdir()
        build_environment = _clean_environment(
            synthetic_data_root,
            synthetic_home=synthetic_home / "build",
            synthetic_temp=synthetic_temp / "build",
        )
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheel_dir),
                str(PROJECT_ROOT),
            ],
            cwd=clean_workspace,
            env=build_environment,
        )

        wheels = sorted(wheel_dir.glob("pkv_kernel-*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            payload = set(archive.namelist())
            metadata_name = next(name for name in payload if name.endswith(".dist-info/METADATA"))
            metadata = archive.read(metadata_name).decode("utf-8")
            metadata_fields = Parser().parsestr(metadata)

        assert metadata_fields["Name"] == "pkv-kernel"
        assert tuple(metadata_fields.get_all("Requires-Dist", ())) == EXPECTED_REQUIRES_DIST
        assert {
            "pkv_kernel/__init__.py",
            "pkv_kernel/contracts.py",
            "pkv_kernel/lifecycle.py",
        }.issubset(payload)
        # ``src`` is retained only as the parent package for the Core modules.
        # Its repository ``main.py`` is the CLI entrypoint and must never leak
        # into a Kernel-only wheel, where it could dynamically import src.cli.
        assert "src/main.py" not in payload
        resource_payload = frozenset(
            name for name in payload if name.startswith(WHEEL_RESOURCE_PREFIX) and not name.endswith("/")
        )
        assert resource_payload == EXPECTED_WHEEL_RESOURCE_PAYLOAD
        lowered_payload = {name.casefold() for name in payload}
        assert not any(
            forbidden in name
            for name in lowered_payload
            for forbidden in ("config/local.yaml", "src/gui", "src/cli", "src/mcp", "pyside6", "qasync")
        )

        install_root = clean_workspace / "clean-install"
        venv_root = install_root / "venv"
        # The venv is clean of project code while intentionally inheriting the
        # already provisioned offline test interpreter's third-party dependencies.
        # Do not switch this to a dependency-closure test: K1b proves package/
        # source isolation, not dependency-index or wheelhouse availability.
        venv.EnvBuilder(with_pip=True, system_site_packages=True, clear=True).create(venv_root)
        interpreter = _venv_python(venv_root)
        assert interpreter.is_file()
        install_environment = _clean_environment(
            synthetic_data_root,
            synthetic_home=synthetic_home / "probe",
            synthetic_temp=synthetic_temp / "probe",
        )
        _run(
            [
                str(interpreter),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(wheel),
            ],
            cwd=install_root,
            env=install_environment,
        )

        probe = """
import json
import importlib.util
import os
import socket
import sys
from pathlib import Path

network_attempts = []

def _block_network(kind):
    def blocked(*args, **kwargs):
        network_attempts.append(kind)
        raise RuntimeError("outbound network is forbidden during the K1b probe")
    return blocked

_socket_type = socket.socket

class _NoNetworkSocket(_socket_type):
    def connect(self, *args, **kwargs):
        return _block_network("socket.connect")(*args, **kwargs)

    def connect_ex(self, *args, **kwargs):
        return _block_network("socket.connect_ex")(*args, **kwargs)

socket.socket = _NoNetworkSocket
socket.create_connection = _block_network("socket.create_connection")
socket.getaddrinfo = _block_network("socket.getaddrinfo")
socket.gethostbyname = _block_network("socket.gethostbyname")
socket.gethostbyname_ex = _block_network("socket.gethostbyname_ex")

# The clean venv lives in a checkout-external disposable workspace.  Guard the
# parent profile's PKV subtree and separately assert every Config-derived path
# uses the probe's synthetic home.
real_profile_config = Path(os.path.abspath(os.path.normpath(__REAL_PROFILE_CONFIG__)))
real_profile_accesses = []

def _is_real_profile_config_path(value):
    try:
        candidate = Path(os.path.abspath(os.path.normpath(os.fspath(value))))
    except (TypeError, ValueError):
        return False
    try:
        return candidate.is_relative_to(real_profile_config)
    except ValueError:
        return False

def _guard_real_profile_config(name, operation):
    def guarded(path, *args, **kwargs):
        if _is_real_profile_config_path(path):
            real_profile_accesses.append(name)
            raise RuntimeError("real PKV profile access is forbidden during the K1b probe")
        return operation(path, *args, **kwargs)
    return guarded

# Guard the parent profile's PKV subtree before importing the SDK for both
# content reads and existence/stat fallbacks.
os.stat = _guard_real_profile_config("os.stat", os.stat)
os.lstat = _guard_real_profile_config("os.lstat", os.lstat)
os.path.exists = _guard_real_profile_config("os.path.exists", os.path.exists)
os.path.lexists = _guard_real_profile_config("os.path.lexists", os.path.lexists)
os.path.isdir = _guard_real_profile_config("os.path.isdir", os.path.isdir)

def _forbid_real_profile_config(event, args):
    if event not in {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename", "os.replace"}:
        return
    if not args or not isinstance(args[0], (str, bytes, os.PathLike)):
        return
    if _is_real_profile_config_path(args[0]):
        real_profile_accesses.append(event)
        raise RuntimeError("real PKV profile access is forbidden during the K1b probe")

sys.addaudithook(_forbid_real_profile_config)

import pkv_kernel

capabilities = pkv_kernel.require_kernel_compatibility(
    minimum_sdk_version=pkv_kernel.__version__,
    maximum_sdk_version=pkv_kernel.__version__,
    required_capabilities=(
        "kernel.lifecycle.v1",
        "kernel.runtime-lifecycle.v1",
        "kernel.configuration-snapshot-reload.v1",
    ),
)
inspection = pkv_kernel.lifecycle.inspect_runtime()
plan = pkv_kernel.lifecycle.plan_runtime(inspection)
kernel = pkv_kernel.bootstrap_kernel()
print(json.dumps({
    "api_version": capabilities.api_version,
    "sdk_version": capabilities.sdk_version,
    "generation": kernel.configuration_generation,
    "kernel_file": str(Path(pkv_kernel.__file__).resolve()),
    "facade_file": str(Path(importlib.util.find_spec("src.kernel.facade").origin).resolve()),
    "resource_root": str(kernel.config.layout.resources_root.resolve()),
    "home": str(Path.home().resolve()),
    "profile_root": str(kernel.config.layout.profile_root.resolve()),
    "user_config_path": str(kernel.config.user_config_path.resolve()),
    "data_root": str(kernel.config.data_root.resolve()),
    "runtime_config_path": str(kernel.config.runtime_config_path.resolve()),
    "lifecycle_readiness": inspection.readiness,
    "lifecycle_plan_id": plan.plan_id,
    "lifecycle_file": str(Path(pkv_kernel.lifecycle.__file__).resolve()),
    "network_attempts": network_attempts,
    "real_profile_accesses": real_profile_accesses,
    "sys_path": [str(Path(value).resolve()) for value in sys.path if value],
}, sort_keys=True))
""".replace("__REAL_PROFILE_CONFIG__", json.dumps(str(real_profile / ".pkv")))
        probe_result = _run(
            [str(interpreter), "-I", "-c", probe],
            cwd=install_root,
            env=install_environment,
        )
        evidence = json.loads(probe_result.stdout)
        project_root = PROJECT_ROOT.resolve()
        venv_resolved = venv_root.resolve()
        kernel_file = Path(evidence["kernel_file"])
        facade_file = Path(evidence["facade_file"])
        lifecycle_file = Path(evidence["lifecycle_file"])
        resource_root = Path(evidence["resource_root"])

        assert not venv_resolved.is_relative_to(project_root)
        assert kernel_file.is_relative_to(venv_resolved)
        assert facade_file.is_relative_to(venv_resolved)
        assert lifecycle_file.is_relative_to(venv_resolved)
        assert resource_root.is_relative_to(venv_resolved)
        assert resource_root.name == "_resources"
        assert f"Version: {evidence['sdk_version']}" in metadata
        assert evidence["api_version"] == "1.0.0"
        assert evidence["generation"] >= 1
        assert evidence["lifecycle_readiness"] == "setup_required"
        assert isinstance(evidence["lifecycle_plan_id"], str)
        assert len(evidence["lifecycle_plan_id"]) == 64
        assert all(not path.is_relative_to(project_root) for path in map(Path, evidence["sys_path"]))
        expected_probe_home = (synthetic_home / "probe").resolve()
        expected_profile_root = expected_probe_home / ".pkv"
        assert Path(evidence["home"]) == expected_probe_home
        assert Path(evidence["profile_root"]) == expected_profile_root
        assert Path(evidence["user_config_path"]) == expected_profile_root / "config.yaml"
        assert Path(evidence["data_root"]) == synthetic_data_root.resolve()
        assert Path(evidence["runtime_config_path"]) == (
            synthetic_data_root / "config" / "local.yaml"
        ).resolve()
        assert not Path(evidence["profile_root"]).is_relative_to(real_profile)
        assert not Path(evidence["user_config_path"]).is_relative_to(real_profile)
        assert evidence["real_profile_accesses"] == []
        assert evidence["network_attempts"] == []
