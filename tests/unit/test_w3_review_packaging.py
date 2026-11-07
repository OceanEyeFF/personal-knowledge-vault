"""W1-W3 review regression: frozen migration identity uses bundled build facts.

The real spec runs with lightweight PyInstaller classes, so the test exercises
its resource generation without building a native executable or requiring a
release artifact. Runtime database checks remain real and use synthetic roots.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sys
from types import ModuleType, SimpleNamespace

import pytest

from src.runtime.errors import ErrorCode, PKVRuntimeError
from src.storage import migration_manager as migration_module
from src.storage.migration_manager import DatabaseState, MigrationManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = PROJECT_ROOT / "packaging" / "pkv.spec"
CODE_RESOURCES = ("src/outcomes/identity.py", "src/storage/outcome_migration.py")
FINGERPRINT_FILE = Path("src/migration-code-fingerprints.json")
pytestmark = pytest.mark.packaging_contract


def _build_spec_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    *, project_root: Path = PROJECT_ROOT, analysis_hook=None,
) -> Path:
    """Run the actual spec and materialize exactly its project DATA closure."""

    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_data_files = lambda *args, **kwargs: []
    hooks.collect_submodules = lambda *args, **kwargs: []
    hooks.copy_metadata = lambda *args, **kwargs: []

    def analysis(*args, **kwargs):
        result = SimpleNamespace(
            pure=[
                (Path(name).with_suffix("").as_posix().replace("/", "."), str(project_root / name), "PYMODULE")
                for name in CODE_RESOURCES
            ] + [("unrelated_namespace", None, "PYMODULE")],
            scripts=[], binaries=[],
            datas=[
                ((Path(destination) / Path(source).name).as_posix(), source, "DATA")
                for source, destination in kwargs["datas"]
            ],
        )
        if analysis_hook is not None:
            analysis_hook(result)
        return result

    with monkeypatch.context() as scoped:
        scoped.setitem(sys.modules, "PyInstaller", ModuleType("PyInstaller"))
        scoped.setitem(sys.modules, "PyInstaller.utils", ModuleType("PyInstaller.utils"))
        scoped.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
        spec = project_root / "packaging/pkv.spec"
        namespace = runpy.run_path(str(spec), init_globals={
            "SPEC": str(spec), "workpath": str(tmp_path / "build"),
            "Analysis": analysis, "PYZ": lambda *args, **kwargs: None,
            "EXE": lambda *args, **kwargs: None,
            "COLLECT": lambda *args, **kwargs: args[3],
        })
    frozen_root = tmp_path / "onedir" / "_internal"
    for name, source, kind in namespace["coll"]:
        assert kind == "DATA"
        destination = frozen_root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return frozen_root


def _copy_build_inputs(tmp_path: Path) -> Path:
    root = tmp_path / "build-inputs"
    manifest = json.loads((PROJECT_ROOT / "packaging/runtime-resources.json").read_text(encoding="utf-8"))
    patterns = [
        "packaging/pkv.spec", "packaging/runtime-resources.json",
        *manifest["include_globs"], *CODE_RESOURCES,
    ]
    for pattern in patterns:
        for path in PROJECT_ROOT.glob(pattern):
            if path.is_file():
                target = root / path.relative_to(PROJECT_ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
    return root


def _freeze_module(monkeypatch: pytest.MonkeyPatch, frozen_root: Path) -> None:
    # PyiFrozenLoader publishes this logical __file__ while code lives in PYZ.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        migration_module, "__file__", str(frozen_root / "src/storage/migration_manager.py")
    )


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "ready"])
def test_frozen_database_inspection_matches_source_identity(tmp_path, monkeypatch, existing):
    frozen_root = _build_spec_resources(tmp_path, monkeypatch)
    db = tmp_path / "data/db/knowledge_vault.db"
    manager = MigrationManager(db, frozen_root / "scripts/migrations")
    if existing:
        manager.initialize_fresh()
    source_inspection = manager.inspect_database()
    original_database = db.read_bytes() if existing else None

    _freeze_module(monkeypatch, frozen_root)
    frozen_inspection = manager.inspect_database()
    assert frozen_inspection.state is (DatabaseState.READY if existing else DatabaseState.FRESH)
    assert frozen_inspection.migration_manifest_sha256 == source_inspection.migration_manifest_sha256
    assert (db.read_bytes() if db.exists() else None) == original_database
    assert not any(frozen_root.rglob("*.py")), "release payload must not contain raw Python source"
    if not existing:
        assert manager.initialize_fresh().state is DatabaseState.READY
        assert manager.inspect_database().migration_manifest_sha256 == source_inspection.migration_manifest_sha256


@pytest.mark.parametrize("corruption", ["missing", "schema", "extra", "missing_resource", "extra_resource", "duplicate_resource", "invalid_hash", "duplicate_key", "extra_item_key", "oversized"])
def test_frozen_code_fingerprint_rejects_missing_or_malformed_resource(tmp_path, monkeypatch, corruption):
    frozen_root = _build_spec_resources(tmp_path, monkeypatch)
    fingerprint = frozen_root / FINGERPRINT_FILE
    payload = json.loads(fingerprint.read_text(encoding="utf-8"))
    if corruption == "missing":
        fingerprint.unlink()
    elif corruption == "duplicate_key":
        fingerprint.write_text('{"schema":"pkv.migration-code.v1",' + json.dumps(payload)[1:], encoding="utf-8")
    elif corruption == "oversized":
        fingerprint.write_text(" " * 4097, encoding="utf-8")
    else:
        if corruption == "schema":
            payload["schema"] = "future"
        elif corruption == "extra":
            payload["extra"] = True
        elif corruption == "missing_resource":
            payload["resources"].pop()
        elif corruption == "extra_resource":
            payload["resources"].append({"resource": "unknown.py", "sha256": "a" * 64})
        elif corruption == "duplicate_resource":
            payload["resources"][1] = dict(payload["resources"][0])
        elif corruption == "invalid_hash":
            payload["resources"][0]["sha256"] = "A" * 64
        elif corruption == "extra_item_key":
            payload["resources"][0]["extra"] = True
        fingerprint.write_text(json.dumps(payload), encoding="utf-8")
    _freeze_module(monkeypatch, frozen_root)
    db = tmp_path / "data/db/knowledge_vault.db"
    with pytest.raises(PKVRuntimeError) as caught:
        MigrationManager(db, frozen_root / "scripts/migrations").inspect_database()
    assert caught.value.code is ErrorCode.RESOURCE_NOT_READABLE
    assert not db.exists()


@pytest.mark.parametrize("resource", CODE_RESOURCES)
def test_frozen_code_fingerprint_drift_rejects_existing_confirmation(tmp_path, monkeypatch, resource):
    from src.runtime import confirm_runtime_plan, execute_runtime_plan, inspect_runtime, plan_runtime
    from tests.unit.test_runtime_lifecycle import _configured, _FakeProbe

    frozen_root = _build_spec_resources(tmp_path, monkeypatch)
    layout, config = _configured(tmp_path / "runtime")
    _freeze_module(monkeypatch, frozen_root)
    inspection = inspect_runtime(config)
    plan = plan_runtime(inspection)
    confirmation = confirm_runtime_plan(plan, allow_network=True)
    fingerprint = frozen_root / FINGERPRINT_FILE
    payload = json.loads(fingerprint.read_text(encoding="utf-8"))
    entry = next(item for item in payload["resources"] if item["resource"] == resource)
    entry["sha256"] = "a" * 64 if entry["sha256"] != "a" * 64 else "b" * 64
    fingerprint.write_text(json.dumps(payload), encoding="utf-8")
    changed = inspect_runtime(config)
    assert changed.revision != inspection.revision
    assert plan_runtime(changed).plan_id != plan.plan_id
    probe = _FakeProbe()
    with pytest.raises(PKVRuntimeError) as caught:
        execute_runtime_plan(plan, confirmation, provider_probe=probe)
    assert caught.value.code is ErrorCode.RUNTIME_PLAN_STALE
    assert probe.calls == []
    assert not layout.user_data_root.exists()


@pytest.mark.parametrize("mismatch", ["changed_source", "unexpected_source", "missing_module"])
def test_fingerprint_builder_rejects_analysis_input_mismatch(tmp_path, monkeypatch, mismatch):
    project = _copy_build_inputs(tmp_path)

    def alter_analysis(analysis):
        if mismatch == "changed_source":
            source = project / CODE_RESOURCES[0]
            source.write_bytes(source.read_bytes() + b"\n# changed during Analysis\n")
        elif mismatch == "unexpected_source":
            module, source, kind = analysis.pure[0]
            different = tmp_path / "other-source.py"
            shutil.copy2(source, different)
            analysis.pure[0] = (module, str(different), kind)
        else:
            analysis.pure.pop(0)

    with pytest.raises(ValueError, match="migration code differs"):
        _build_spec_resources(tmp_path, monkeypatch, project_root=project, analysis_hook=alter_analysis)


def test_fingerprint_builder_recomputes_changed_source_without_raw_source_payload(tmp_path, monkeypatch):
    project = _copy_build_inputs(tmp_path)
    first = _build_spec_resources(tmp_path / "first", monkeypatch, project_root=project)
    source = project / CODE_RESOURCES[0]
    source.write_bytes(source.read_bytes() + b"\n# new build input\n")
    second = _build_spec_resources(tmp_path / "second", monkeypatch, project_root=project)
    before = json.loads((first / FINGERPRINT_FILE).read_text(encoding="utf-8"))
    after = json.loads((second / FINGERPRINT_FILE).read_text(encoding="utf-8"))
    assert before != after
    updated = next(item for item in after["resources"] if item["resource"] == CODE_RESOURCES[0])
    assert updated["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not any(second.rglob("*.py"))


@pytest.mark.parametrize("names", [[], [CODE_RESOURCES[0]], [CODE_RESOURCES[0]] * 2, [*CODE_RESOURCES, "unknown.py"]])
def test_fingerprint_builder_requires_complete_closed_source_allowlist(tmp_path, monkeypatch, names):
    project = _copy_build_inputs(tmp_path)
    path = project / "packaging/runtime-resources.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["migration_code_resources"] = names
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="migration code resource allowlist"):
        _build_spec_resources(tmp_path, monkeypatch, project_root=project)
