# LMA Studio

LMA Studio (LIF-MS Annotation Studio) is a local desktop application for human-assisted LIF-MS annotation review.

It opens its own native desktop window, lets users create or open project directories, review QC anchors and cell-level LIF-MS candidates, and export accepted annotations as CSV. The application stores review state in each project's local SQLite database.

## Current Scope

- Create a new annotation project from 2-4 LIF raw files, 1 MS raw file, and a single-cell event-coordinate CSV containing `scan_start_time`, `UMAP1`, and `UMAP2`; unrelated source columns are allowed and ignored.
- Open an existing project containing preprocessing parquet tables and `annotation_app/annotations/annotation.sqlite`.
- Configure channel detector, scientific identity, and cell-annotation role. The new-project UI automatically groups Green and Red inputs onto their shared physical time axes instead of asking users to type internal axis names.
- Configure ordered project-level `calibration_protocol` reference segments. Each segment may be Green-only, Red-only, or Red+Green; a read-only raw-peak scan can suggest boundaries, but never confirms them.
- Configure post-run QC independently as `signature`, `scheduled_windows`, or `disabled`.
- Estimate one calibration shift per physical axis, so same-axis channels such as G1/G2 pool evidence into one `green_axis` shift without requiring simultaneous peaks.
- Review three explicit UI stages: segmented front calibration, generic unlabeled post-run delta, and event annotation / QC survey.
- Restrict every new project's third-stage candidates and manual writes to a canonical `ms_event_id` whitelist matched from the coordinate CSV.
- Open a separate synchronized UMAP window. Its colors are derived only from current accepted SQLite relations; clicking a point focuses the same event in the main track window.
- Export a compact 16-column CSV intended for downstream cell labeling. Internal hashes, ambiguity payloads, model metadata, and audit details stay in SQLite rather than bloating the CSV.
- Use external raw input references to avoid copying large MS files into every project.
- Open v0.3 G2+R1 projects through a read-only compatibility adapter without rewriting their manifest or changing existing annotation semantics.
- Invalidate dependent time models and third-stage results explicitly when a frozen-model input changes, while preserving manual annotation history.

## Desktop Releases

The latest formal GitHub Release remains v0.3.0. The current development candidate is v0.4.0-rc1 and must not be published as a formal Release before Windows user acceptance.

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
LMA_STUDIO_VERSION=v0.4.0-rc1 bash packaging/macos/build_macos.sh
```

Manual `workflow_dispatch` builds upload candidate artifacts only. Formal GitHub Release publication is tag-triggered and is intentionally deferred until user acceptance.
