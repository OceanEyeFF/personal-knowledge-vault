# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build graph for the headless PKV entrypoints."""

import hashlib
import json
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


PROJECT_ROOT = Path(SPEC).resolve().parent.parent
PACKAGING_DIR = PROJECT_ROOT / "packaging"
RESOURCE_MANIFEST = PACKAGING_DIR / "runtime-resources.json"
ENTRYPOINT = PACKAGING_DIR / "pkv_entrypoint.py"
BUILD_ONLY_MODULE_EXCLUDES = frozenset(
    {
        "mypy",
        "mypy_extensions",
        "packaging",
        "pydantic.mypy",
        "pydantic.v1.mypy",
        "setuptools",
        "wheel",
        "_distutils_hack",
    }
)


def _load_manifest():
    with RESOURCE_MANIFEST.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("schema_version") != 2:
        raise ValueError("unsupported runtime resource manifest schema")
    return manifest


def _project_datas(manifest):
    datas = []
    for pattern in manifest["include_globs"]:
        matches = sorted(path for path in PROJECT_ROOT.glob(pattern) if path.is_file())
        if not matches:
            raise ValueError(f"runtime resource pattern has no files: {pattern}")
        for path in matches:
            destination = path.relative_to(PROJECT_ROOT).parent.as_posix()
            datas.append((str(path), destination))
    return datas


def _migration_code_inputs(manifest):
    """Capture the exact source bytes whose compiled modules enter this build."""

    required = {"src/outcomes/identity.py", "src/storage/outcome_migration.py"}
    names = manifest["migration_code_resources"]
    if type(names) is not list or len(names) != len(required) or set(names) != required:
        raise ValueError("migration code resource allowlist is incomplete or unsupported")
    return {
        name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest()
        for name in names
    }


def _migration_code_data(analysis, captured):
    """Bind the source identities without placing raw Python in the payload."""

    modules = {name: source for name, source, _ in analysis.pure}
    for name, digest in captured.items():
        source = PROJECT_ROOT / name
        module = Path(name).with_suffix("").as_posix().replace("/", ".")
        if (
            modules.get(module) is None
            or Path(modules[module]).resolve() != source.resolve()
            or hashlib.sha256(source.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("migration code differs from the analyzed build input: " + name)
    payload = {
        "schema": "pkv.migration-code.v1",
        "resources": [
            {"resource": name, "sha256": digest}
            for name, digest in sorted(captured.items())
        ],
    }
    generated = Path(workpath) / "migration-code-fingerprints.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return ("src/migration-code-fingerprints.json", str(generated), "DATA")


manifest = _load_manifest()
pyinstaller = manifest["pyinstaller"]
datas = _project_datas(manifest)
hiddenimports = list(pyinstaller["hiddenimports"])
excludes = set(pyinstaller["excludes"])
missing_build_only_excludes = BUILD_ONLY_MODULE_EXCLUDES - excludes
if missing_build_only_excludes:
    raise ValueError(
        "runtime resource manifest is missing build-only excludes: "
        + ", ".join(sorted(missing_build_only_excludes))
    )
python_options = [("X utf8", None, "OPTION")]
migration_code_inputs = _migration_code_inputs(manifest)

for package in pyinstaller["collect_submodules"]:
    hiddenimports.extend(collect_submodules(package))
for metadata_name in pyinstaller["copy_metadata"]:
    datas.extend(copy_metadata(metadata_name, recursive=False))
for collection in pyinstaller["collect_data_files"]:
    datas.extend(
        collect_data_files(
            collection["package"],
            includes=collection["includes"],
        )
    )

# A single Analysis/PYZ ensures the two headless executables share an identical module
# and native-library closure.  COLLECT then emits one Windows x64 onedir tree.
a = Analysis(
    [str(ENTRYPOINT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[str(PACKAGING_DIR / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=sorted(excludes),
    noarchive=False,
    optimize=0,
)
a.datas.append(_migration_code_data(a, migration_code_inputs))
pyz = PYZ(a.pure)

pkv_cli = EXE(
    pyz,
    a.scripts,
    python_options,
    exclude_binaries=True,
    name="pkv",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)
pkv_mcp = EXE(
    pyz,
    a.scripts,
    python_options,
    exclude_binaries=True,
    name="pkv-mcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    contents_directory="_internal",
)

coll = COLLECT(
    pkv_cli,
    pkv_mcp,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pkv",
)
