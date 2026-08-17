# LMA Studio v0.4.9 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.9-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.9-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.9

- Press `D` in `Events > Cell pair` to run `Save pending`. Pending relations remain reviewable in SQLite/Track but do not classify UMAP points, enter the main CSV, reserve an MS event, or train a model until accepted.
- The compact left-hand workflow is now `S` for `Select peaks`, `A` for `Save pair`, and `D` for `Save pending`. Form fields, modals, bootstrap, modifier-key combinations, key repeats, and busy states remain protected from shortcut activation.
- Track and UMAP now consume one shared high-contrast signal palette. Accepted UMAP cell colors therefore exactly match their G1/G2/R1/R2 Track identities, while unknown and QC state colors remain separate.
- The new palette also separates MS760 and MS782 more clearly from the LIF channels. It is presentation-only: no manifest, SQLite, time-model, candidate, peak-calling, event-map, or CSV schema changes are required.

## Retained interaction behavior

- The shortcut reminder uses separate stable lines so it remains readable in the narrow annotation sidebar.
- Entering an MS760 time in UMAP now outlines matching points in red without changing the current zoom or pan.
- Double-clicking a coordinate-mapped MS760 peak in Track opens or restores the native UMAP window and outlines the same canonical event in red. The UMAP view remains unchanged; LIF and MS782 peaks are not treated as unique UMAP event identifiers.
- Peak calling, calibration matching, event-map binding, time models, project schemas, CSV columns, and accepted/pending annotation semantics are unchanged.

## Compatibility

- Existing formal v0.4.0-v0.4.8 projects using the current adaptive two-tier LIF peak standard open from their manifest paths without migration or preprocessing reruns. Existing candidates, annotations, pending reviews, frozen models, and directory layouts remain intact.
- Opening an existing project never reruns the MS caller. The project continues to use its manifest-bound peak and event tables.
- Projects made with the retired fixed-threshold peak standard remain intentionally rejected before writes. Rebuild those projects in a new empty directory from the original LIF/MS/event-list inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than individual parquet, SQLite, or coordinate files.

## Validation

- Focused interaction tests cover accepted/pending shortcut guards and discoverability, the shared Track/UMAP palette, UMAP time lookup without view mutation, Track-to-UMAP event identity, and the native-window message handshake.
- The user-accepted Windows candidate and release source passed 382 automated tests (2 platform-conditional skips), JavaScript syntax checks, packaged scientific-binary provenance checks, ABI validation, and the packaged runtime probe before formal publication.
- The formal tag workflow binds package names to the source `APP_VERSION`, verifies both platform archives against their SHA256 files, and publishes only after Windows x64 and macOS ARM64 jobs both pass.

## Data policy

The source repository and application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
