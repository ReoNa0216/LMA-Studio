# LMA Studio v0.4.13 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.13-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.13-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.13

- Accepted Calibration anchors are now treated as direct project evidence. Valid Green-axis and Red-axis shifts beyond the automatic unlabeled search range are retained instead of being clipped at 60 seconds.
- Green-axis and Red-axis fits remain independent, while G1/G2 continue to share one Green axis and R1/R2 continue to share one Red axis. MS remains the fixed reference.
- Robust fitting now reports how many accepted relations were used for each axis. A fit is blocked when only a minority of accepted relations support it; excluded or changed relations remain visible in a collapsed review list instead of being silently discarded.
- Calibration fine-tune sliders now derive their range from the project's accepted evidence and current model, so large valid shifts can be previewed and applied without arbitrary truncation.
- Applying a revised Calibration model preserves manual annotations and audit history while explicitly invalidating downstream MS time-difference results for recomputation.
- Narrow-sidebar status text is shorter and uses balanced wrapping. User-facing middle-dot separators were removed from the main and UMAP interfaces, with regression checks preventing their reintroduction.

## Validation

- The automated suite covers large accepted shifts, shared physical axes, robust outlier review, insufficient-support blocking, dynamic fine-tune ranges, downstream invalidation, annotation preservation, UMAP behavior, and desktop-window interactions.
- The Windows development bundle passed the packaged runtime probe and scientific-binary provenance and ABI audit.
- The formal tag workflow builds Windows x64 and macOS ARM64 packages independently, verifies both archives against their SHA256 files, and publishes only after both jobs succeed.

## Data policy

The source repository and application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
