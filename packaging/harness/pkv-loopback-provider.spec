# -*- mode: python ; coding: utf-8 -*-
"""One-file, standard-library-only W3 loopback harness."""

from pathlib import Path


harness_root = Path(SPECPATH).resolve()
entrypoint = harness_root / "loopback_provider.py"

a = Analysis(
    [str(entrypoint)],
    pathex=[str(harness_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "hnswlib",
        "httpx",
        "numpy",
        "openai",
        "pytest",
        "src",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="pkv-loopback-provider",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
