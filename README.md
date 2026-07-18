# LMA Studio

LMA Studio (LIF-MS Annotation Studio) is a local desktop application for human-assisted LIF-MS annotation review.

It opens its own native desktop window, lets users create or open project directories, review QC anchors and cell-level LIF-MS candidates, and export accepted annotations as CSV. The application stores review state in each project's local SQLite database.

## Current Scope

- Create a new annotation project from 2-4 LIF raw files, 1 MS raw file, and a required single-cell event-coordinate CSV.
- Open an existing project containing preprocessing parquet tables and `annotation_app/annotations/annotation.sqlite`.
- Assign each LIF channel independently to QC calibration, cell annotation, both roles, or neither role. QC anchors must cover every physical time axis and include at least one green and one red detector.
- Estimate one calibration shift per physical axis, so same-axis channels such as G1/G2 reinforce one green-axis estimate instead of creating extra shift parameters.
- Review three UI stages: QC calibration, local post-QC MS shift calibration, and a merged event-annotation stage containing explicit QC/cell filters.
- Restrict every new project's third-stage candidates and manual writes to a canonical `ms_event_id` whitelist matched from the coordinate CSV.
- Open a separate synchronized UMAP window. Its colors are derived only from current accepted SQLite relations; clicking a point focuses the same event in the main track window.
- Export accepted annotations with project-name timestamped CSV filenames. Third-stage rows include UMAP coordinates and the canonical map SHA; early QC rows leave those fields blank.
- Use external raw input references to avoid copying large MS files into every project.
- Open schema-v1 projects without rewriting them. Projects without a map keep the legacy unfiltered workflow and can attach one map exactly once after compatibility checks.

## Desktop Releases

The latest formal GitHub Release is v0.3.0.

Windows x64:

```text
LMAStudio\LMAStudio.exe
```

The first screen intentionally starts without a loaded project. Choose `新建项目` or `打开项目` from the initialization page.

The Windows desktop build uses Microsoft Edge WebView2 Runtime. Windows 11 normally includes it; the application reports a clear startup error if it is unavailable.

macOS Apple Silicon:

1. Unzip `LMA-Studio-<version>-macos-arm64.zip`.
2. Move `LMA Studio.app` to Applications or another writable location.
3. Control-click the app and choose **Open** on first launch if Gatekeeper warns that the package is from an unidentified developer.

The macOS ARM64 build uses the native Cocoa/WebKit backend. It remains unsigned and not notarized until an Apple Developer ID is configured, so Windows remains the fully locally validated release platform.

## Project Data Policy

LMA Studio projects are directory-based. A project contains generated intermediate parquet tables, a project manifest, and the annotation SQLite database. Raw inputs can be referenced externally, which is recommended for large MS text files.

The application package does not include user raw data, project SQLite databases, canonical/source UMAP CSV files, parquet tables, exported CSV files, author CSV files, or h5ad files.

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

The macOS ARM64 build runs on an Apple Silicon host or the repository GitHub Actions workflow:

```bash
LMA_STUDIO_VERSION=v0.3.0 bash packaging/macos/build_macos.sh
```
