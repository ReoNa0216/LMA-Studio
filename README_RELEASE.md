# LMA Studio v0.4.5 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.5-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.5-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.5

- Event-coordinate CSV import now requires only `scan_start_time`; `UMAP1` and `UMAP2` are optional and may be attached later. Extra columns such as `Type`, `batch`, and `CellNumber` remain ignored as scientific evidence.
- The established automatic MS760 event caller remains unchanged at ±12 ppm. When a coordinate-list row has no core event, a separate manual-review lane may add one resolved local maximum inside ±15 ppm, but only on zero-inflated traces with measurable local background and a robust upper-fence threshold.
- These roster-supported events are restricted to whitelisted manual Cell-pair review. They never train calibration, time-difference, post-run QC, automatic Cell candidates, or QC refits.
- New event maps use strict apex matching first and a unique, local peak-shape fallback bounded to 0.25 seconds. Ambiguous relations remain rejected. Historical schema-1 v0.4 projects replay their original half-height binding semantics without rewriting data.
- Extended-lane review events now carry a consistent displayed trace, intensity ratio, actual PC(34:1) m/z, compact CSV value, and provenance audit row.

## Compatibility

- Existing formal v0.4.0-v0.4.4 projects using the current adaptive two-tier LIF peak standard open from their manifest paths without migration or preprocessing reruns. Existing candidates, annotations, pending reviews, frozen models, and directory layouts remain intact.
- Opening an existing project never reruns the revised MS caller. The new event behavior applies only when creating a project from raw inputs.
- Projects made with the retired fixed-threshold peak standard remain intentionally rejected before writes. Rebuild those projects in a new empty directory from the original LIF/MS/event-list inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than individual parquet, SQLite, or coordinate files.

## Validation

- Final-code temporary projects were rebuilt from all five supplied HSC2 datasets. The one-to-one event-list bindings were HSC 451/451, CLP 626/626, MPP 735/735, LSK 497/497, and LK 706/706.
- Their unchanged core-event counts were HSC 825, CLP 911, MPP 1,219, LSK 565, and LK 1,135. Roster-supported manual-review events were 25, 55, 0, 49, and 99 respectively.
- Automated adversarial tests cover core/support isolation, zero-inflated thresholding, 12–15 ppm edge evidence, ambiguity rejection, bounded shape matching, legacy schema-1 replay, manual-only use, display/export consistency, atomic creation, v0.4 project compatibility, and both desktop packaging boundaries.
- The formal tag workflow binds package names to the source `APP_VERSION`, verifies both platform archives against their SHA256 files, and publishes only after Windows x64 and macOS ARM64 jobs both pass.

## Data policy

The source repository and application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
