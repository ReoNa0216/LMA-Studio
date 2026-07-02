# LMA Studio

LMA Studio (LIF-MS Annotation Studio) is a local Windows application for human-assisted LIF-MS annotation review.

It opens a browser-based interface on `127.0.0.1`, lets users create or open project directories, review QC anchors and cell-level LIF-MS candidates, and export accepted annotations as CSV. The application stores review state in each project's local SQLite database.

## Current Scope

- Create a new annotation project from 3 LIF raw files and 1 MS raw file.
- Open an existing project containing preprocessing parquet tables and `annotation_app/annotations/annotation.sqlite`.
- Configure project-level LIF channel layout and QC anchor channels.
- Review four workflow stages: QC calibration, local post-QC MS shift calibration, QC survey, and cell annotation.
- Export accepted annotations with project-name timestamped CSV filenames.
- Use external raw input references to avoid copying large MS files into every project.

## Windows Release

Download the Windows x64 zip from GitHub Releases, unzip it, and run:

```text
LMAStudio\LMAStudio.exe
```

Then open:

```text
http://127.0.0.1:8050/
```

The first screen intentionally starts without a loaded project. Choose `新建项目` or `打开项目` from the initialization page.

## Project Data Policy

LMA Studio projects are directory-based. A project contains generated intermediate parquet tables, a project manifest, and the annotation SQLite database. Raw inputs can be referenced externally, which is recommended for large MS text files.

The application package does not include user raw data, project SQLite databases, exported CSV files, author CSV files, or h5ad files.

## Developer Build

The Windows build expects Python 3.11 and prefers a conda environment named `lifms_annotation_win`.

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
```

Build output:

```text
dist\LMAStudio\LMAStudio.exe
```

Run tests:

```powershell
python -m unittest discover -s tests
```
