# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


repo_root = Path(SPECPATH).parents[1]
release_version = os.environ.get("LMA_STUDIO_VERSION", "0.4.0-rc3").lstrip("v")
bundle_version = release_version.split("-", 1)[0]


def production_submodule(name):
    parts = name.split(".")
    return not any(
        part in {"tests", "conftest", "benchmark"}
        or part.startswith("test_")
        or part.startswith("_test")
        for part in parts
    )


datas = [
    (str(repo_root / "scripts/v3/lif_peak_detection.py"), "scripts/v3"),
    (str(repo_root / "scripts/v3/project_protocol.py"), "scripts/v3"),
    (str(repo_root / "scripts/v3/run_v3_01_lif_trace_physical_qc.py"), "scripts/v3"),
    (str(repo_root / "scripts/v3/run_v3_02_ms_event_calling.py"), "scripts/v3"),
]
datas += collect_data_files("matplotlib")
datas += collect_data_files("webview", subdir="lib")
datas += collect_data_files("webview", subdir="js")

binaries = []
binaries += collect_dynamic_libs("pyarrow")
binaries += collect_dynamic_libs("scipy")

hiddenimports = []
hiddenimports += collect_submodules("pyarrow", filter=production_submodule)
hiddenimports += collect_submodules("scipy", filter=production_submodule)
hiddenimports += [
    "AppKit",
    "Foundation",
    "Quartz",
    "Security",
    "UniformTypeIdentifiers",
    "WebKit",
    "matplotlib.backends.backend_agg",
    "objc",
    "pandas._libs.tslibs.timedeltas",
    "webview.platforms.cocoa",
]


a = Analysis(
    [str(repo_root / "annotation_app/desktop.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": "Agg"}},
    runtime_hooks=[],
    excludes=[
        "anndata",
        "scanpy",
        "sklearn",
        "seaborn",
        "jupyter",
        "IPython",
        "pytest",
        "_pytest",
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "webview.platforms.android",
        "webview.platforms.cef",
        "webview.platforms.edgechromium",
        "webview.platforms.gtk",
        "webview.platforms.mshtml",
        "webview.platforms.qt",
        "webview.platforms.winforms",
    ],
    noarchive=False,
    optimize=0,
)
a.datas = [
    entry
    for entry in a.datas
    if not str(entry[0]).replace("\\", "/").startswith("pyarrow/tests/")
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LMAStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    icon=str(repo_root / "packaging/macos/LMAStudio.icns"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LMAStudio",
)
app = BUNDLE(
    coll,
    name="LMA Studio.app",
    icon=str(repo_root / "packaging/macos/LMAStudio.icns"),
    bundle_identifier="io.github.reona0216.LMAStudio",
    info_plist={
        "CFBundleDisplayName": "LMA Studio",
        "CFBundleShortVersionString": bundle_version,
        "CFBundleVersion": bundle_version,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Copyright 2026 LMA Studio contributors",
    },
)
