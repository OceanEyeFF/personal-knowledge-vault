"""K1b contracts for an offline local ``pkv-kernel`` wheel.

The test deliberately builds without dependency resolution, installs into a
new source-free venv in a repository-external temporary workspace, and launches
an isolated interpreter.  PKV runtime data remains below the isolated test root.
It is evidence of source-tree independence, not a release/PyPI workflow.
"""

from __future__ import annotations

from email.parser import Parser
import hashlib
from importlib import metadata as distribution_metadata
import importlib.util
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
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
        "pkv_kernel/_resources/config/price-cards.yaml",
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
        "pkv_kernel/_resources/scripts/migrations/011_add_ai_automation_ledger.sql",
        "pkv_kernel/_resources/scripts/migrations/012_add_r4_content_submission.sql",
        "pkv_kernel/_resources/scripts/migrations/013_add_traceable_outcomes.sql",
        "pkv_kernel/_resources/scripts/migrations/014_add_lifecycle_consistency.sql",
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
    "tzdata>=2024.1",
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
        encoding="utf-8",
        errors="replace",
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
        "PYTHONTZPATH",
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


def _copy_verified_conda_timezones(source_prefix: Path, target_prefix: Path) -> dict:
    """Provide an explicit offline IANA dependency fixture for Conda venvs.

    Conda's Windows Python derives TZPATH from each interpreter's sys.prefix;
    system-site-packages does not carry the base environment's share/zoneinfo.
    Copy only manifest-verified distribution bytes, never point the isolated
    probe back at the host prefix or manufacture a Python tzdata distribution.
    """
    records = list((source_prefix / "conda-meta").glob("tzdata-*.json"))
    assert len(records) == 1, "Offline wheel probe requires one verified IANA distribution"
    manifest_bytes = records[0].read_bytes()
    record = json.loads(manifest_bytes)
    assert record["name"] == "tzdata"
    assert type(record["version"]) is str and record["version"]
    assert type(record["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
    assert record["paths_data"]["paths_version"] == 1
    entries = record["paths_data"]["paths"]
    assert type(entries) is list and entries
    verified = []
    seen = set()
    for entry in entries:
        relative = entry["_path"]
        assert type(relative) is str and "\\" not in relative
        path = PurePosixPath(relative)
        assert path.as_posix() == relative
        assert not path.is_absolute() and path.parts[:2] == ("share", "zoneinfo")
        assert len(path.parts) >= 3 and all(part not in {"", ".", ".."} and ":" not in part for part in path.parts)
        assert relative not in seen
        seen.add(relative)
        expected = entry.get("sha256_in_prefix", entry.get("sha256"))
        assert type(expected) is str and re.fullmatch(r"[0-9a-f]{64}", expected)
        assert type(entry["size_in_bytes"]) is int and entry["size_in_bytes"] >= 0
        source = source_prefix.joinpath(*path.parts)
        assert source.resolve().is_relative_to(source_prefix.resolve())
        assert not source.is_symlink() and source.is_file()
        data = source.read_bytes()
        assert len(data) == entry["size_in_bytes"] and hashlib.sha256(data).hexdigest() == expected
        verified.append((path, data, expected))
    assert "share/zoneinfo/UTC" in seen
    # Finish validating the whole dependency before copying any payload.
    for path, data, _ in verified:
        destination = target_prefix.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    payload = [[str(path), digest] for path, _, digest in sorted(verified, key=lambda item: str(item[0]))]
    return {
        "source": "verified-conda-iana-fixture", "version": record["version"],
        "conda_package_sha256": record["sha256"],
        "metadata_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "file_count": len(verified),
        "payload_sha256": hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest(),
        "files": dict(payload),
    }


def _provide_offline_timezone_dependency(venv_root: Path) -> dict:
    if importlib.util.find_spec("tzdata") is not None:
        # This clean venv explicitly shares the already trusted dependency
        # environment, just like the other wheel dependencies in this test.
        return {"source": "installed-python-distribution", "version": distribution_metadata.version("tzdata")}
    return _copy_verified_conda_timezones(Path(sys.prefix), venv_root)


@pytest.mark.parametrize("invalid", ["checksum", "size", "escape", "absolute", "duplicate"])
def test_offline_timezone_fixture_rejects_unverified_distribution_before_copy(tmp_path, invalid):
    source = tmp_path / "source"
    target = tmp_path / "target"
    zone = source / "share" / "zoneinfo" / "UTC"
    zone.parent.mkdir(parents=True)
    data = b"synthetic dependency bytes"
    zone.write_bytes(data)
    entry = {"_path": "share/zoneinfo/UTC", "size_in_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if invalid == "checksum":
        entry["sha256"] = "a" * 64
    elif invalid == "size":
        entry["size_in_bytes"] = len(data) + 1
    elif invalid == "escape":
        entry["_path"] = "share/zoneinfo/../../../outside"
    elif invalid == "absolute":
        entry["_path"] = "/share/zoneinfo/UTC"
    entries = [entry, entry] if invalid == "duplicate" else [entry]
    manifest = source / "conda-meta" / "tzdata-fixture.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"name": "tzdata", "version": "fixture", "sha256": "b" * 64,
                                    "paths_data": {"paths_version": 1, "paths": entries}}), encoding="utf-8")
    with pytest.raises(AssertionError):
        _copy_verified_conda_timezones(source, target)
    assert not target.exists()


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
        timezone_evidence = _provide_offline_timezone_dependency(venv_root)
        (tmp_path / "timezone-dependency.json").write_text(
            json.dumps(timezone_evidence, indent=2, sort_keys=True), encoding="utf-8"
        )
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
import warnings
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

synthetic_user_config = Path.home() / ".pkv" / "config.yaml"
synthetic_user_config.parent.mkdir(parents=True, exist_ok=True)
synthetic_user_config.write_text(json.dumps({
    "ai": {"llm": {"api_key": "synthetic-wheel-key", "base_url": "https://llm.invalid/v1"},
           "embedding": {"api_key": "synthetic-wheel-key", "base_url": "https://embedding.invalid/v1", "dim": 3},
           "embedding_rebuild": {"schema_version": 1, "token_budget": {
        "timezone": "UTC", "daily_total_tokens": 100, "monthly_total_tokens": 1000,
    }}},
}), encoding="utf-8")

import pkv_kernel

capabilities = pkv_kernel.require_kernel_compatibility(
    minimum_sdk_version=pkv_kernel.__version__,
    maximum_sdk_version=pkv_kernel.__version__,
    required_api_major=1,
    required_capabilities=(
        "kernel.lifecycle.v1",
        "kernel.runtime-lifecycle.v1",
        "kernel.configuration-snapshot-reload.v1",
        "kernel.traceable-outcomes.v1",
        "kernel.traceable-outcomes.v2",
        "kernel.embedding-rebuild.v1",
        "kernel.embedding-rebuild.v2",
    ),
)
inspection = pkv_kernel.lifecycle.inspect_runtime()
plan = pkv_kernel.lifecycle.plan_runtime(inspection)
kernel = pkv_kernel.bootstrap_kernel()
# Bootstrap creates the synthetic business database.  Record its missing R2
# snapshot through the public, local-only lifecycle plan before v2 inspection.
snapshot_plan = pkv_kernel.lifecycle.plan_runtime(pkv_kernel.lifecycle.inspect_runtime(kernel.config))
assert not any(action["requires_network"] for action in snapshot_plan.to_dict()["actions"])
pkv_kernel.lifecycle.execute_runtime_plan(
    snapshot_plan, pkv_kernel.lifecycle.confirm_runtime_plan(snapshot_plan),
)
assert kernel.config.user_config_path == synthetic_user_config
assert kernel.config.get("ai.embedding_rebuild.token_budget.daily_total_tokens") == 100
from zoneinfo import ZoneInfo
assert ZoneInfo("UTC").key == "UTC"
embedding_v2_functions = (
    "inspect_embedding_rebuild_v2", "plan_embedding_rebuild_v2",
    "confirm_embedding_rebuild_v2", "execute_embedding_rebuild_v2",
    "inspect_embedding_operation_v2", "plan_embedding_publication_v2",
    "confirm_embedding_publication_v2", "execute_embedding_publication_v2",
)
assert all(callable(getattr(pkv_kernel.embedding, name)) for name in embedding_v2_functions)
embedding_inspection = pkv_kernel.embedding.inspect_embedding_rebuild_v2(kernel.config)
embedding_plan = pkv_kernel.embedding.plan_embedding_rebuild_v2(embedding_inspection)
embedding_confirmation = pkv_kernel.embedding.confirm_embedding_rebuild_v2(embedding_plan)
assert embedding_inspection.schema == "pkv.embedding.inspection.v2"
assert embedding_confirmation.plan_id == embedding_plan.plan_id
assert embedding_confirmation.allow_network is False
try:
    pkv_kernel.embedding.execute_embedding_rebuild_v2(embedding_plan, embedding_confirmation)
except pkv_kernel.PKVRuntimeError as error:
    assert error.code.value == "embedding_rebuild_authorization_required"
else:
    raise AssertionError("Installed SDK executed without network permission")
try:
    pkv_kernel.embedding.inspect_embedding_operation_v2("embedding-rebuild-" + "a" * 32, config=kernel.config)
except pkv_kernel.PKVRuntimeError as error:
    assert error.code.value == "embedding_operation_not_found"
else:
    raise AssertionError("A nonexistent operation must not become executable")
for name in (
    "EmbeddingRebuildInspectionV2", "EmbeddingRebuildPlanV2", "EmbeddingRebuildConfirmationV2",
    "EmbeddingRebuildExecutionV2", "EmbeddingOperationInspectionV2", "EmbeddingPublicationPlanV2",
    "EmbeddingPublicationConfirmationV2",
):
    try:
        getattr(pkv_kernel.embedding, name)()
    except TypeError:
        pass
    else:
        raise AssertionError("Installed v2 handles must be factory-only")
topic = pkv_kernel.outcomes.create_topic(
    title="Installed compatibility build", question="Is the v2 surface installed?", kernel=kernel,
)
bundle = pkv_kernel.outcomes.get_outcome_v2(topic.topic_id, kernel=kernel).to_dict()
assert bundle["schema"] == "pkv.outcome.bundle.v2"
assert bundle["topic"]["input_revision"] == 1
assert bundle["brief"] is None
assert pkv_kernel.outcomes.get_research_brief_v2(topic_id=topic.topic_id, kernel=kernel) is None
confirmation = pkv_kernel.outcomes.ResearchBriefReviewConfirmation(
    outcome_id=bundle["topic"]["outcome_id"], expected_revision=1,
    content_sha256="a" * 64, expected_input_revision=1,
)
assert confirmation.to_dict()["expected_input_revision"] == 1
with warnings.catch_warnings(record=True) as recorded:
    warnings.simplefilter("always", DeprecationWarning)
    try:
        pkv_kernel.outcomes.approve_research_brief(topic_id=topic.topic_id, kernel=kernel)
    except pkv_kernel.PKVRuntimeError as error:
        assert error.code.value == "outcome_approval_blocked"
    else:
        raise AssertionError("An unrendered topic must not be approved")
assert len(recorded) == 1 and recorded[0].category is DeprecationWarning
assert "approve_research_brief_v2" in str(recorded[0].message)
assert "major 2" in str(recorded[0].message)
try:
    pkv_kernel.require_kernel_compatibility(required_api_major=2)
except pkv_kernel.KernelCompatibilityError:
    pass
else:
    raise AssertionError("The compatible build must not claim API major 2")
print(json.dumps({
    "api_version": capabilities.api_version,
    "sdk_version": capabilities.sdk_version,
    "outcome_schema": bundle["schema"],
    "embedding_schema": embedding_inspection.schema,
    "embedding_v2_function_count": len(embedding_v2_functions),
    "embedding_plan_id": embedding_plan.plan_id,
    "embedding_file": str(Path(pkv_kernel.embedding.__file__).resolve()),
    "legacy_review_warning": str(recorded[0].message),
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
        assert evidence["sdk_version"] == "0.10.0"
        assert evidence["outcome_schema"] == "pkv.outcome.bundle.v2"
        assert evidence["embedding_schema"] == "pkv.embedding.inspection.v2"
        assert evidence["embedding_v2_function_count"] == 8
        assert len(evidence["embedding_plan_id"]) == 64
        assert Path(evidence["embedding_file"]).is_relative_to(venv_resolved)
        assert "approve_research_brief_v2" in evidence["legacy_review_warning"]
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
