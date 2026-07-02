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

## Run

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1
```

Then open:

```text
http://127.0.0.1:8050/
```

Optional explicit project launch:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1 -ProjectDir "D:\LMAProjects\Batch03"
```

## Release Principles

- Package only the application code and required preprocessing scripts.
- Do not package user project directories, annotation SQLite databases, exports, raw input files, author CSV files, or h5ad files.
- User data remains under the project directory selected inside LMA Studio.
