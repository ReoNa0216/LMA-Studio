# LMA Studio v0.3.0 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.3.0-windows-x64.zip`: fully validated Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.3.0-macos-arm64.zip`: Apple Silicon build produced and structurally verified on GitHub Actions. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**. A physical Mac GUI acceptance test has not yet been performed.

## Highlights

- New projects support two to four LIF channels, with independent QC, cell, both-role, or disabled assignments per channel.
- QC anchors are derived from physical detector time axes, including shared-axis handling for related channels.
- A canonical five-column single-cell event map binds each new project to an explicit `ms_event_id` whitelist.
- Compatible legacy projects can attach an event map once without rewriting existing project data.
- QC survey and cell annotation are merged into a gated third stage with explicit QC/cell filters.
- Third-stage writes require the current frozen local time model and are checked against the bound event-map whitelist.
- A separate pywebview UMAP window stays synchronized with accepted/rejected events in the main track window.
- UMAP rendering responds to window resizing, includes adaptive UMAP1/UMAP2 axes, and supports pan, zoom, hover, and fit-to-data controls.
- Clicking an event in UMAP opens its containing 2.5-minute track window, for example `50.075` opens `50.00-52.50 min`.
- Accepted-annotation CSV exports include canonical UMAP coordinates and map provenance for third-stage rows.

## Data Policy

The application packages contain no user projects, raw LIF/MS files, SQLite databases, source/canonical event-map CSV files, parquet tables, author CSV files, h5ad files, or exported annotations.

## Validation

Windows validation completed locally:

- 107 automated tests passed, with one expected POSIX-only skip.
- Clean PyInstaller x64 build and native-window startup at the initialization page.
- Packaged pythonnet/CLR import probe both normally and with simulated Internet-zone markers.
- Four-channel synthetic coverage includes QC-only, cell-only, both-role, disabled, and shared-axis configurations.
- `CART_Exp1-3` event-map regression: 417 events = 379 cell + 35 QC + 3 unknown, with zero conflicts.
- Compatibility regressions passed for `Batch03Test`, `CART_Exp1-3`, `CART_Exp2-1`, and `Young_HSC3`.
- Annotation/database/project-file hashes remained unchanged during regression checks.
- Packaged EXE synchronization regression confirmed that UMAP event `50.075` opens the `50.00-52.50 min` track window.

macOS ARM64 validation performed in GitHub Actions:

- Full automated test suite on an Apple Silicon runner.
- PyInstaller `.app` build, bundle/plist validation, ad-hoc code-signature verification, ARM64 executable inspection, and command-line startup smoke test.
