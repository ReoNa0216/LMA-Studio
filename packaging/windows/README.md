# Windows Packaging

This directory contains the Windows packaging scripts for LMA Studio.

## Build

Run from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
```

The script prefers the conda environment named `lifms_annotation_win`. You can also pass a Python executable explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1 -PythonExe "D:\Miniconda\envs\lifms_annotation_win\python.exe"
```

Build output:

```text
dist\LMAStudio\LMAStudio.exe
```

Distribute the whole `dist\LMAStudio` directory or a zip made from that directory. Do not distribute only the single exe; the onedir build also needs bundled DLLs and runtime resources.

The build copies `LMAStudio.exe.config` beside the executable and probes the packaged pythonnet/CLR bridge both normally and with simulated Internet-zone download markers. This guards the common GitHub-download-and-extract startup path.

## Run

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1
```

The executable opens a native LMA Studio window. It starts an internal HTTP server on a random loopback port and stops that server when the window closes.

Optional explicit project launch:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1 -ProjectDir "D:\LMAProjects\Batch03"
```

Normal double-click startup never restores the last project; it always starts at the project selection screen.

## Runtime

The desktop host requires Microsoft Edge WebView2 Runtime. Windows 11 normally includes it. LMA Studio detects a missing runtime before opening the application window and records startup diagnostics under `%LOCALAPPDATA%\LMA Studio\logs`.

## Release Principles

- Package only the application code and required preprocessing scripts.
- Do not package user project directories, annotation SQLite databases, exports, raw input files, source/canonical UMAP CSV files, parquet tables, author CSV files, or h5ad files.
- User data remains under the project directory selected inside LMA Studio.
- The packaged desktop app uses pywebview native file dialogs and does not bundle Tk/Tcl.
