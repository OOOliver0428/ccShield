# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPEC).resolve().parent.parent

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + ["brotli", "ahocorasick"]
)

a = Analysis(
    [str(ROOT / "scripts" / "release.py")],
    pathex=[str(ROOT / "backend"), str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "basedpyright", "ruff"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ccShield",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "ccshield.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ccShield",
)
