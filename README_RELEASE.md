# LMA Studio v0.4.6 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.6-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.6-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.6

- The strict one-to-one event-list gate remains unchanged: every `scan_start_time` row must bind to one distinct eligible MS760 event, or the project is not created.
- A failed new-project import now shows a row-count summary and offers a downloadable UTF-8 diagnostic CSV instead of only a short alert.
- The report contains every source row, with its CSV line number, time, status, stable reason code, Chinese explanation, match method, matched/candidate event IDs, nearest event, and timing offset. It distinguishes normal matches, no eligible event after roster support, multiple candidates, bounded peak-shape rejection, and event reuse conflicts.
- The report is generated only from event times and MS evidence. `Type`, `batch`, `CellNumber`, UMAP coordinates, author labels, source absolute paths, configuration hashes, and model metadata are not copied into it or used scientifically.
- Diagnostic data is returned only with the failed local request. Atomic staging is still rolled back, so downloading a report never leaves a partial project directory or changes an existing project.

## Compatibility

- Existing formal v0.4.0-v0.4.5 projects using the current adaptive two-tier LIF peak standard open from their manifest paths without migration or preprocessing reruns. Existing candidates, annotations, pending reviews, frozen models, and directory layouts remain intact.
- Opening an existing project never reruns the revised MS caller. The new event behavior applies only when creating a project from raw inputs.
- Projects made with the retired fixed-threshold peak standard remain intentionally rejected before writes. Rebuild those projects in a new empty directory from the original LIF/MS/event-list inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than individual parquet, SQLite, or coordinate files.

## Validation

- The v0.4.5 scientific caller and all five successful HSC2 one-to-one bindings remain unchanged. v0.4.6 adds observability at the failed-import boundary rather than relaxing matching criteria.
- Automated tests cover complete row reporting, stable failure classification, privacy-column exclusion, structured HTTP transport, browser download behavior, atomic rollback, core/support isolation, ambiguity rejection, legacy schema-1 replay, v0.4 project compatibility, and desktop packaging boundaries.
- The formal tag workflow binds package names to the source `APP_VERSION`, verifies both platform archives against their SHA256 files, and publishes only after Windows x64 and macOS ARM64 jobs both pass.

## Data policy

The source repository and application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
