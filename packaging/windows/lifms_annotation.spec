# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


repo_root = Path(SPECPATH).parents[1]
python_prefix = Path(sys.prefix)
conda_library = python_prefix / "Library"
conda_bin = conda_library / "bin"
conda_tcl = conda_library / "lib" / "tcl8.6"
conda_tk = conda_library / "lib" / "tk8.6"
conda_libexpat = conda_bin / "libexpat.dll"

datas = [
    (str(repo_root / "scripts/v3/run_v3_01_lif_trace_physical_qc.py"), "scripts/v3"),
    (str(repo_root / "scripts/v3/run_v3_02_ms_event_calling.py"), "scripts/v3"),
]
datas += collect_data_files("matplotlib")

binaries = []
binaries += collect_dynamic_libs("pyarrow")
binaries += collect_dynamic_libs("scipy")
if (conda_bin / "tcl86t.dll").exists() and (conda_bin / "tk86t.dll").exists():
    binaries += [
        (str(conda_bin / "tcl86t.dll"), "."),
        (str(conda_bin / "tk86t.dll"), "."),
    ]
if conda_libexpat.exists():
    binaries += [(str(conda_libexpat), ".")]

hiddenimports = []
hiddenimports += collect_submodules("pyarrow")
hiddenimports += collect_submodules("scipy")
hiddenimports += [
    "matplotlib.backends.backend_agg",
    "pandas._libs.tslibs.timedeltas",
]


a = Analysis(
    [str(repo_root / "annotation_app/app.py")],
    pathex=[str(repo_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "anndata",
        "scanpy",
        "sklearn",
        "seaborn",
        "jupyter",
        "IPython",
    ],
    noarchive=False,
    optimize=0,
)

if (conda_bin / "tcl86t.dll").exists() and (conda_bin / "tk86t.dll").exists():
    a.binaries = [
        item
        for item in a.binaries
        if Path(str(item[0])).name.lower() not in {"tcl86t.dll", "tk86t.dll"}
    ]
    a.binaries += [
        ("tcl86t.dll", str(conda_bin / "tcl86t.dll"), "BINARY"),
        ("tk86t.dll", str(conda_bin / "tk86t.dll"), "BINARY"),
    ]
if conda_libexpat.exists():
    a.binaries = [
        item
        for item in a.binaries
        if Path(str(item[0])).name.lower() != "libexpat.dll"
    ]
    a.binaries += [("libexpat.dll", str(conda_libexpat), "BINARY")]
if conda_tcl.exists() and conda_tk.exists():
    a.datas = [
        item
        for item in a.datas
        if not str(item[0]).startswith(("_tcl_data", "_tk_data"))
    ]
    a.datas += Tree(str(conda_tcl), prefix="_tcl_data")
    a.datas += Tree(str(conda_tk), prefix="_tk_data")
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
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
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
