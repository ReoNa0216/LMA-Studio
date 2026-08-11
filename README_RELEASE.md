# LMA Studio v0.4.1 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.1-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.1-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## Highlights

- Existing formal v0.4.0 projects remain manifest-compatible: opening them does not rerun peak recognition, migrate their historical directory layout, or replace annotations and frozen time models.
- New projects use a compact portable layout with `data/`, `annotations/`, `provenance/`, and optional `diagnostics/` directories. Project roots can be renamed or moved as a complete directory.
- Project configuration can validate and import an alternative UMAP coordinate CSV for exactly the same MS-event population. Switching pre/post batch-correction coordinates updates UMAP and exported coordinates only; annotations, peaks, and the frozen model remain unchanged. Invalid replacements roll back atomically.
- UMAP can locate and outline points by MS760 time. Its query controls and the configuration reference-window rows now wrap safely in narrow Windows and macOS WebKit layouts.
- The macOS desktop pre-creates and reuses its hidden UMAP child window before entering the Cocoa event loop, avoiding the prior unresponsive UMAP button while keeping the main window interactive.
- New raw preprocessing uses a robust background-body PC34 threshold and an inclusive ±12 ppm extraction tolerance. Existing v0.4.0 projects keep their manifest-bound MS event tables; the revised caller runs only when raw inputs are rebuilt.
- One adaptive two-tier LIF peak standard remains project-bound. Core peaks drive automatic calibration, time-difference estimation, QC, candidate generation, and model fitting; weak peaks are optional evidence for manual Cell pair annotation only.
- Two to four LIF channels can independently serve as front `QC anchor`, Events-stage `Cell pair`, both roles, or neither. Ordered front windows support green-only, red-only, or combined evidence, and dense evidence uses deterministic non-crossing matching.
- Post-run QC remains independent of front calibration and can be `Off`, `QC signature`, or `Scheduled windows`.
- The main CSV remains a compact 16-column full event roster. Unannotated events use `Type=unknown`; detailed audit and model metadata remain in SQLite.

## Compatibility Boundary

- Projects created by the formal v0.4.0 release and bound to the current peak-recognition standard can be opened, reviewed, edited, exported, and used with the new UMAP coordinate switch without rebuilding.
- Projects made with the retired peak-recognition standard are rejected before writes. Rebuild those projects in a new empty directory from their original LIF/MS/coordinate inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than moving individual parquet, SQLite, or coordinate files.

## Data Policy

The application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, or exported annotations.

## Validation

- The complete automated suite, embedded JavaScript syntax checks, packaging syntax checks, and bundled-runtime probes pass on the release source.
- Windows validation uses complete temporary copies of `Lin-_LSK`; the original project and `HSC1_data` remain unchanged. The packaged EXE opens the v0.4.0 layout, saves a Cell pair, exports the exact 16-column roster, and switches all 971 UMAP points while preserving protected annotation/model content.
- Raw MS compatibility checks operate on manifest-declared table copies and retain every established PC34 apex in CART_Exp1-3, CART_Exp2-1, Young_HSC3, and Lin−/LSK fixtures.
- macOS ARM64 validation runs the complete tests, validates the `.app` bundle and plist, checks ARM64 architecture, applies an ad-hoc signature, and runs the scientific/runtime startup probe.
