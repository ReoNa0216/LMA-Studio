# LMA Studio

LMA Studio (LIF-MS Annotation Studio) is a local desktop application for human-assisted LIF-MS annotation review.

It opens its own native desktop window, lets users create or open project directories, review QC anchors and cell-level LIF-MS candidates, and export a downstream cell roster as CSV. The application stores review state in each project's local SQLite database.

## Current Scope

- Create a new annotation project from 2-4 LIF raw files, 1 MS raw file, and a single-cell event-coordinate CSV containing `scan_start_time`, `UMAP1`, and `UMAP2`; unrelated source columns are allowed and ignored.
- Open an existing project containing the manifest-bound preprocessing parquet tables and annotation SQLite path; both the compact current layout and unchanged v0.4.0 paths are supported.
- Configure each channel's signal color, sample label, and cell-annotation role. A channel may simultaneously serve as a front `QC anchor` and an Events-stage `Cell pair` source; the two uses are independent. Shared acquisition-time groups are assigned automatically, so users never type internal axis names.
- Use one project-wide adaptive two-tier LIF peak standard. High-confidence peaks are the only evidence used by automatic calibration, time-difference estimation, QC, candidate generation, and model fitting. Weak candidate peaks are optional display evidence for manual cell pairing only.
- Configure ordered front reference windows using green-only, red-only, or combined red/green evidence. A read-only raw-peak scan can suggest boundaries but never confirms them. Unconfirmed windows can be opened as a raw-track draft while calibration and downstream stages remain locked.
- Configure later QC independently as `Off`, `QC signature`, or `Scheduled windows`, with a short explanation of the intended experimental scenario in the UI.
- Estimate one calibration translation per shared signal-time group, so same-color channels such as G1/G2 pool evidence without requiring simultaneous peaks.
- Match dense calibration evidence with a deterministic order-preserving sequence matcher, preventing physically impossible crossed peak assignments while retaining explicit ambiguity handling.
- Review three explicit UI stages: segmented front calibration, generic unlabeled post-run delta, and event annotation / QC survey.
- Restrict every new project's third-stage candidates and manual writes to a canonical `ms_event_id` whitelist matched from the coordinate CSV.
- Open a separate synchronized UMAP window. Its colors are derived only from current accepted SQLite relations; the QC legend is omitted when the active event stage has no QC, clicking a point focuses the same event in the main track window, and an MS760 time lookup can outline matching points without changing annotations. A different coordinate CSV for the same MS-event population can be validated and imported from project configuration to switch, for example, between pre- and post-batch-correction views without changing annotations or the time model.
- Export a compact 16-column full event roster intended for downstream cell labeling. Unannotated events use `Type=unknown`; internal hashes, ambiguity payloads, model metadata, and audit details stay in SQLite rather than bloating the CSV.
- Create new projects with a compact manifest-bound layout: `data/`, `annotations/`, `provenance/`, and `diagnostics/`, without exposing internal pipeline-version directories. Existing v0.4.0 projects retain their original manifest paths and are never auto-migrated.
- Use external raw input references to avoid copying large MS files into every project.
- Reject projects created with the retired peak-recognition standard before any project write. The original project remains unchanged; use a new empty directory and the original LIF/MS/coordinate inputs to rebuild under the current standard.
- Invalidate dependent time models and third-stage results explicitly when a frozen-model input changes, while preserving manual annotation history.

## Desktop Releases

The current formal release is v0.4.1. Existing v0.4.0 projects remain manifest-compatible and are not migrated when opened.

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

LMA Studio projects are directory-based. New projects put runtime tables in `data/`, annotation state and exports in `annotations/`, reproducibility records in `provenance/`, and optional review material in `diagnostics/`. Raw inputs can be referenced externally, which is recommended for large MS text files.

The application package does not include user raw data, project SQLite databases, canonical/source UMAP CSV files, parquet tables, exported CSV files, author CSV files, or h5ad files.

Runtime paths always come from `lifms_project.json`; code must not infer them from a folder name. Existing v0.4.0 projects can therefore keep their historical paths unchanged, while newly created projects use the compact layout. Never move a table or SQLite file independently: its path and digest are part of the project binding. Close LMA Studio before copying or archiving a project, and share the complete root directory. The root directory itself may be renamed.

Detailed layout and compatibility boundary: [docs/project_directory_layout.md](docs/project_directory_layout.md).

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
LMA_STUDIO_VERSION=v0.4.2-rc1 bash packaging/macos/build_macos.sh
```

Manual `workflow_dispatch` builds upload non-publishing test artifacts. Formal GitHub Release publication is tag-triggered after all release gates pass.

Release notes: [README_RELEASE.md](README_RELEASE.md). Current renamed-project walkthrough: [docs/Lin-_LSK_v0.4.1_UAT.md](docs/Lin-_LSK_v0.4.1_UAT.md).
