"""Artifact-only runner contract tests.

This module intentionally uses only the Python standard library plus pytest.
It never imports the product source tree.  The selected lane must provide a
synthetic installed artifact and probe through explicit external paths.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest


pytestmark = pytest.mark.artifact

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPOSITORY_ROOT / "scripts" / "run-artifact-e2e.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
REQUIRED_ENVIRONMENT = {
    "artifact_root": "PKV_ARTIFACT_ROOT",
    "entrypoint": "PKV_ARTIFACT_ENTRYPOINT",
    "manifest": "PKV_ARTIFACT_MANIFEST",
    "fixture": "PKV_ARTIFACT_FIXTURE",
    "evidence_root": "PKV_ARTIFACT_EVIDENCE_ROOT",
}
FORBIDDEN_CHILD_ENVIRONMENT = {
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
    "PKV_TEST_PROJECT_ROOT",
}


def _is_within(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.normcase(str(candidate.resolve(strict=False)))
    root_text = os.path.normcase(str(root.resolve(strict=False)))
    try:
        return os.path.commonpath([candidate_text, root_text]) == root_text
    except ValueError:
        return False


@pytest.fixture(scope="module", autouse=True)
def artifact_lane_inputs() -> dict[str, Path]:
    missing = [name for name in REQUIRED_ENVIRONMENT.values() if not os.getenv(name)]
    if missing:
        pytest.fail(
            "artifact lane requires explicit environment inputs: "
            + ", ".join(sorted(missing)),
            pytrace=False,
        )
    if POWERSHELL is None:
        pytest.fail("artifact lane requires powershell.exe or pwsh", pytrace=False)

    inputs = {
        key: Path(os.environ[environment_name]).resolve(strict=False)
        for key, environment_name in REQUIRED_ENVIRONMENT.items()
    }
    required_existing = ("artifact_root", "entrypoint", "manifest", "fixture")
    absent = [key for key in required_existing if not inputs[key].exists()]
    if absent:
        pytest.fail(
            "explicit artifact lane inputs do not exist: " + ", ".join(absent),
            pytrace=False,
        )
    for key, value in inputs.items():
        if _is_within(value, REPOSITORY_ROOT):
            pytest.fail(f"{key} must be outside the repository: {value}", pytrace=False)
    if not _is_within(inputs["entrypoint"], inputs["artifact_root"]):
        pytest.fail("entrypoint must be inside artifact root", pytrace=False)
    if not _is_within(inputs["manifest"], inputs["artifact_root"]):
        pytest.fail("manifest must be inside artifact root", pytrace=False)
    if _is_within(inputs["evidence_root"], inputs["artifact_root"]) or _is_within(
        inputs["artifact_root"], inputs["evidence_root"]
    ):
        pytest.fail("artifact root and evidence root must be disjoint", pytrace=False)
    return inputs


def _runner_command(
    inputs: dict[str, Path],
    *,
    run_id: str,
    probe: bool = False,
    require_harness: bool = False,
    overrides: dict[str, Path] | None = None,
    probe_arguments: list[str] | None = None,
    probe_timeout_seconds: int | None = None,
) -> list[str]:
    selected = dict(inputs)
    selected.update(overrides or {})
    command = [
        str(POWERSHELL),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(RUNNER),
        "-ArtifactRoot",
        str(selected["artifact_root"]),
        "-EntryPoint",
        str(selected["entrypoint"]),
        "-ManifestPath",
        str(selected["manifest"]),
        "-FixturePath",
        str(selected["fixture"]),
        "-EvidenceRoot",
        str(selected["evidence_root"]),
        "-RunId",
        run_id,
    ]
    if probe:
        command.append("-RunContractProbe")
    if require_harness:
        command.append("-RequireHarness")
    if probe_arguments:
        command.extend(["-ProbeArguments", *probe_arguments])
    if probe_timeout_seconds is not None:
        command.extend(["-ProbeTimeoutSeconds", str(probe_timeout_seconds)])
    return command


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    assert cwd.is_dir()
    assert not _is_within(cwd, REPOSITORY_ROOT)
    return subprocess.run(
        command,
        cwd=cwd,
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _new_run_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _assert_text_excludes_repository(value: object) -> None:
    repository = os.path.normcase(str(REPOSITORY_ROOT.resolve())).replace("/", "\\")
    text = os.path.normcase(str(value)).replace("/", "\\")
    assert repository not in text


def _remove_empty_scratch(*paths: Path) -> None:
    for path in paths:
        if path.exists():
            path.rmdir()


def _process_exists(process_id: int) -> bool:
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, process_id
    )
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def test_runner_fails_closed_without_explicit_parameters(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    result = _run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
        ],
        cwd=artifact_lane_inputs["evidence_root"].parent,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("key", "suffix"),
    [
        ("artifact_root", "missing-artifact"),
        ("entrypoint", "missing-entrypoint.exe"),
        ("manifest", "missing-manifest.json"),
        ("fixture", "missing-fixture.json"),
    ],
)
def test_runner_fails_closed_when_required_path_is_missing(
    artifact_lane_inputs: dict[str, Path], key: str, suffix: str
) -> None:
    missing = artifact_lane_inputs["evidence_root"].parent / suffix
    assert not missing.exists()
    result = _run(
        _runner_command(
            artifact_lane_inputs,
            run_id=_new_run_id("missing"),
            overrides={key: missing},
        ),
        cwd=artifact_lane_inputs["evidence_root"].parent,
    )

    assert result.returncode != 0
    assert "does not exist" in result.stderr


def test_runner_fails_closed_when_required_harness_is_absent(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    result = _run(
        _runner_command(
            artifact_lane_inputs,
            run_id=_new_run_id("harness"),
            require_harness=True,
        ),
        cwd=artifact_lane_inputs["evidence_root"].parent,
    )

    assert result.returncode != 0
    assert "requires an explicit external harness" in result.stderr


def test_runner_rejects_repository_contained_artifact_input(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    result = _run(
        _runner_command(
            artifact_lane_inputs,
            run_id=_new_run_id("repository"),
            overrides={
                "artifact_root": REPOSITORY_ROOT,
                "entrypoint": RUNNER,
                "manifest": REPOSITORY_ROOT / "pytest.ini",
            },
        ),
        cwd=artifact_lane_inputs["evidence_root"].parent,
    )

    assert result.returncode != 0
    assert "Artifact root must resolve outside the repository" in result.stderr


def test_runner_rejects_junction_in_external_artifact_path_chain(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    command_processor = shutil.which("cmd.exe")
    if command_processor is None:
        pytest.fail("junction negative test requires cmd.exe", pytrace=False)
    junction = (
        artifact_lane_inputs["artifact_root"].parent
        / f".pkv-runner-junction-{uuid.uuid4().hex}"
    )
    creation = subprocess.run(
        [
            command_processor,
            "/d",
            "/c",
            "mklink",
            "/J",
            str(junction),
            str(REPOSITORY_ROOT),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
        check=False,
    )
    if creation.returncode != 0 or not junction.exists():
        pytest.fail(
            "could not create required junction negative fixture: "
            + creation.stderr
            + creation.stdout,
            pytrace=False,
        )

    try:
        result = _run(
            _runner_command(
                artifact_lane_inputs,
                run_id=_new_run_id("junction"),
                overrides={
                    # The leaf is a normal target directory; its parent segment
                    # is the external junction back into the repository.
                    "artifact_root": junction / "scripts",
                    "entrypoint": junction / "scripts" / "run-artifact-e2e.ps1",
                    "manifest": junction / "pytest.ini",
                },
            ),
            cwd=artifact_lane_inputs["evidence_root"].parent,
        )
        assert result.returncode != 0
        assert "Unsafe ReparsePoint rejected for Artifact root" in result.stderr
    finally:
        removal = subprocess.run(
            [command_processor, "/d", "/c", "rmdir", str(junction)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            check=False,
        )
        if removal.returncode != 0 or junction.exists():
            pytest.fail(
                "could not remove junction negative fixture: "
                + removal.stderr
                + removal.stdout,
                pytrace=False,
            )


def test_runner_rejects_external_file_hardlink(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    scratch_root = (
        artifact_lane_inputs["artifact_root"].parent
        / f".pkv-runner-hardlink-{uuid.uuid4().hex}"
    )
    scratch_artifact = scratch_root / "artifact"
    scratch_artifact.mkdir(parents=True)
    copied_entrypoint = scratch_artifact / "synthetic-probe.ps1"
    hardlinked_manifest = scratch_artifact / "manifest.json"
    hardlink_source = scratch_root / "hardlink-source.json"
    shutil.copyfile(artifact_lane_inputs["entrypoint"], copied_entrypoint)
    hardlink_source.write_text(
        '{"schema_version":"pkv.synthetic-hardlink.v1"}\n',
        encoding="utf-8",
    )
    try:
        try:
            os.link(hardlink_source, hardlinked_manifest)
        except OSError as exc:
            pytest.fail(
                f"could not create required hardlink negative fixture: {exc}",
                pytrace=False,
            )
        assert os.stat(hardlink_source).st_nlink >= 2
        assert os.stat(hardlinked_manifest).st_ino == os.stat(hardlink_source).st_ino

        result = _run(
            _runner_command(
                artifact_lane_inputs,
                run_id=_new_run_id("hardlink"),
                overrides={
                    "artifact_root": scratch_artifact,
                    "entrypoint": copied_entrypoint,
                    "manifest": hardlinked_manifest,
                },
            ),
            cwd=artifact_lane_inputs["evidence_root"].parent,
        )
        assert result.returncode != 0
        assert "Unsafe HardLink rejected for Artifact manifest" in result.stderr
    finally:
        if hardlinked_manifest.exists():
            hardlinked_manifest.unlink()
        if copied_entrypoint.exists():
            copied_entrypoint.unlink()
        if hardlink_source.exists():
            hardlink_source.unlink()
        _remove_empty_scratch(scratch_artifact, scratch_root)


def test_contract_probe_timeout_is_bounded_and_terminates_process_tree(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    scratch_root = (
        artifact_lane_inputs["artifact_root"].parent
        / f".pkv-runner-timeout-{uuid.uuid4().hex}"
    )
    scratch_artifact = scratch_root / "artifact"
    scratch_artifact.mkdir(parents=True)
    timeout_probe = scratch_artifact / "timeout-probe.ps1"
    copied_manifest = scratch_artifact / "manifest.json"
    timeout_probe.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$hostPath = (Get-Process -Id $PID -ErrorAction Stop).Path",
                "$child = Start-Process -FilePath $hostPath -ArgumentList "
                "@('-NoLogo','-NoProfile','-NonInteractive','-Command',"
                "'Start-Sleep -Seconds 120') -WindowStyle Hidden -PassThru",
                "$pidPath = [System.IO.Path]::ChangeExtension("
                "$env:PKV_ARTIFACT_PROBE_OUTPUT, '.child.pid')",
                "[System.IO.File]::WriteAllText($pidPath, [string]$child.Id)",
                "Start-Sleep -Seconds 120",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    shutil.copyfile(artifact_lane_inputs["manifest"], copied_manifest)
    run_id = _new_run_id("timeout-tree")
    child_process_id: int | None = None
    try:
        started = time.monotonic()
        result = _run(
            _runner_command(
                artifact_lane_inputs,
                run_id=run_id,
                probe=True,
                probe_timeout_seconds=1,
                overrides={
                    "artifact_root": scratch_artifact,
                    "entrypoint": timeout_probe,
                    "manifest": copied_manifest,
                },
            ),
            cwd=artifact_lane_inputs["evidence_root"].parent,
        )
        elapsed = time.monotonic() - started

        assert result.returncode != 0
        assert "Contract probe timed out after 1 seconds" in result.stderr
        assert elapsed < 10, f"internal timeout was not bounded: {elapsed:.2f}s"

        pid_path = (
            artifact_lane_inputs["evidence_root"]
            / "runs"
            / run_id
            / "contract-probe.child.pid"
        )
        assert pid_path.is_file(), "timeout probe did not record its child PID"
        child_process_id = int(pid_path.read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 3
        while _process_exists(child_process_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _process_exists(child_process_id), (
            f"timed-out probe left child process {child_process_id} running"
        )
    finally:
        if child_process_id is not None and _process_exists(child_process_id):
            subprocess.run(
                ["taskkill.exe", "/PID", str(child_process_id), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        if timeout_probe.exists():
            timeout_probe.unlink()
        if copied_manifest.exists():
            copied_manifest.unlink()
        _remove_empty_scratch(scratch_artifact, scratch_root)


def test_preflight_never_claims_release_verification(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    run_id = _new_run_id("preflight")
    result = _run(
        _runner_command(artifact_lane_inputs, run_id=run_id),
        cwd=artifact_lane_inputs["evidence_root"].parent,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "preflight_passed"
    assert payload["contract_probe"] == {"status": "not_requested", "exit_code": None}
    assert "artifact_verified" not in json.dumps(payload, sort_keys=True).lower()
    result_path = (
        artifact_lane_inputs["evidence_root"]
        / "runs"
        / run_id
        / "preflight-result.json"
    )
    assert json.loads(result_path.read_text(encoding="utf-8")) == payload


def test_contract_probe_runs_with_external_cwd_and_sanitized_environment(
    artifact_lane_inputs: dict[str, Path],
) -> None:
    run_id = _new_run_id("probe")
    result = _run(
        _runner_command(artifact_lane_inputs, run_id=run_id, probe=True),
        cwd=artifact_lane_inputs["evidence_root"].parent,
    )

    assert result.returncode == 0, result.stderr
    preflight = json.loads(result.stdout)
    assert preflight["status"] == "contract_probe_passed"
    assert preflight["contract_probe"] == {"status": "passed", "exit_code": 0}

    run_root = artifact_lane_inputs["evidence_root"] / "runs" / run_id
    probe_path = run_root / "contract-probe.json"
    assert probe_path.is_file(), "synthetic probe did not emit its observation"
    observation = json.loads(probe_path.read_text(encoding="utf-8-sig"))

    observed_cwd = Path(observation["cwd"]).resolve()
    assert _is_within(observed_cwd, artifact_lane_inputs["evidence_root"])
    assert not _is_within(observed_cwd, REPOSITORY_ROOT)
    assert not _is_within(observed_cwd, artifact_lane_inputs["artifact_root"])
    assert Path(preflight["working_directory"]).resolve() == observed_cwd
    _assert_text_excludes_repository(observation["cwd"])

    observed_argv = observation["argv"]
    assert isinstance(observed_argv, list)
    for argument in observed_argv:
        _assert_text_excludes_repository(argument)

    observed_environment = {
        str(name).upper(): str(value)
        for name, value in observation["environment"].items()
    }
    assert not (FORBIDDEN_CHILD_ENVIRONMENT & observed_environment.keys())
    for value in observed_environment.values():
        _assert_text_excludes_repository(value)
    assert Path(observed_environment["PKV_ARTIFACT_ROOT"]).resolve() == artifact_lane_inputs[
        "artifact_root"
    ].resolve()
    assert Path(observed_environment["PKV_ARTIFACT_MANIFEST"]).resolve() == artifact_lane_inputs[
        "manifest"
    ].resolve()
    assert Path(observed_environment["PKV_ARTIFACT_FIXTURE"]).resolve() == artifact_lane_inputs[
        "fixture"
    ].resolve()
    assert "artifact_verified" not in json.dumps(preflight, sort_keys=True).lower()
