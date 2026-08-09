# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


repo_root = Path(SPECPATH).parents[1]


def preferred_runtime_dll(name):
    """Select DLLs from the interpreter environment, never a parent Conda env."""

    candidates = [
        Path(sys.prefix) / "Library/bin" / name,
        Path(sys.prefix) / "DLLs" / name,
        Path(sys.prefix) / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return (str(candidate), ".")
    return None


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

binaries = [
    item
    for item in (
        preferred_runtime_dll("libexpat.dll"),
        preferred_runtime_dll("libcrypto-3-x64.dll"),
        preferred_runtime_dll("libssl-3-x64.dll"),
        preferred_runtime_dll("liblzma.dll"),
        preferred_runtime_dll("libbz2.dll"),
        preferred_runtime_dll("ffi-8.dll"),
        preferred_runtime_dll("sqlite3.dll"),
    )
    if item is not None
]
binaries += collect_dynamic_libs("pyarrow")
binaries += collect_dynamic_libs("scipy")
binaries += collect_dynamic_libs("webview")

hiddenimports = []
hiddenimports += collect_submodules("pyarrow", filter=production_submodule)
hiddenimports += collect_submodules("scipy", filter=production_submodule)
hiddenimports += [
    "matplotlib.backends.backend_agg",
    "pandas._libs.tslibs.timedeltas",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
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
        "webview.platforms.cef",
        "webview.platforms.android",
        "webview.platforms.cocoa",
        "webview.platforms.gtk",
        "webview.platforms.qt",
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
    upx=True,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(repo_root / "packaging/windows/LMAStudio.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="LMAStudio",
)
