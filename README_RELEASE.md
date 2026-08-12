# LMA Studio v0.4.4 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.4-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.4-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.4

- Coordinate CSV import still tries the strict event-apex tolerance first. If no apex qualifies, a source time may now bind to one unique MS-event peak-support interval. This fixes valid shoulder-time rows without widening the global tolerance or allowing nearest-neighbor guessing.
- The atomic project-staging recheck now retains event-support boundaries, so packaged project creation evaluates exactly the same scientific evidence before and after preprocessing.
- New raw-MS preprocessing is more sensitive on traces with a positive continuous PC34 background. A robust upper-tail baseline estimate can cap an over-conservative background-body threshold; zero-inflated traces retain the previous estimator.
- Ambiguous or overlapping event support remains rejected. Coordinate row counts, cell identities, `Type`, `batch`, and other author labels never influence event calling or threshold selection.

## Compatibility

- Project schema, LIF peak tables, time models, annotation SQLite schema, portable directory layout, UMAP coordinate map, and the compact 16-column CSV contract are unchanged from v0.4.3.
- Existing formal v0.4.0-v0.4.3 projects that use the current peak-recognition standard open in place without migration or preprocessing reruns. Existing candidates, annotations, pending reviews, and frozen models remain intact.
- The revised MS caller applies only when creating a project again from raw inputs. Rebuilding may recover additional low-amplitude events; opening an existing project continues to use its manifest-bound event tables.
- Projects made with the retired peak-recognition standard remain intentionally rejected before writes. Rebuild those projects in a new empty directory from the original LIF/MS/coordinate inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than individual parquet, SQLite, or coordinate files.

## Validation

- The automated suite covers strict-apex priority, unique-support fallback, overlapping-support rejection, positive-background sensitivity, staging consistency, project compatibility, CSV output, and both desktop packaging boundaries.
- The Windows candidate was exercised through the packaged HTTP/UI backend against the real CLP and LK LIF/MS inputs: CLP imported all 950 coordinate rows and LK imported all 646 coordinate rows, with the original files unchanged.
- The packaged Windows candidate also opened a complete v0.4.0-layout `Lin-_LSK` project copy, preserved all 971 event-map rows, and exported the unchanged 16-column roster without modifying the original project.
- The formal tag workflow rebuilds both Windows x64 and macOS ARM64 packages from the same source commit and publishes the Release only after both packaged-runtime gates pass.

## Data policy

The application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, or exported annotations.
