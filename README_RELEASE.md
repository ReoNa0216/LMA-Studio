# LMA Studio v0.4.10 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.10-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.10-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.10

- Front Calibration now separates close PC34 maxima by their measured local saddle: a return to background or a deep, prominence-supported saddle establishes two independent peaks, while shallow tail ripples remain one unresolved complex. This pairwise rule prevents a chain of small shoulders from swallowing a later resolved peak.
- A core MS event remains available for inspection and manual work, but automatic Calibration now requires both height and prominence to clear the event caller's own local thresholds by a two-fold margin. This dimensionless rule prevents a nearly exact-time background extremum from stealing an anchor from a clear peak without using dataset-specific absolute intensity cutoffs.
- Calibration markers now encode use directly: orange outline for the current candidate, dark outline for eligible unmatched evidence, hollow markers for review-only near-background events, and gray markers for secondary maxima in one unresolved complex. Equally credible MS alternatives remain explicit ambiguity and are not drawn as one decisive connector. The MS782 support trace does not display PC34 event circles as independently called QC peaks.
- An explicitly selected ambiguous QC relation is now saved as a manual anchor instead of being routed back through the blocked automatic candidate. Press `F` in QC anchor mode to run `Save anchor`; the existing `S`, `A`, and `D` shortcuts keep their Select, Save pair, and Save pending meanings.
- Calibration review writes now restore the refit controls after their window refresh. The first click on `Preview accepted anchors` is no longer swallowed after accepting, rejecting, clearing, batch-reviewing, or manually saving an anchor, and the preview button reports its in-progress state.
- macOS staging cleanup now tolerates an AppleDouble `._*` sidecar that disappears while an incomplete project is being removed. Cleanup can no longer replace the real import error with a misleading `._input_manifest.csv` error.
- Zero-inflated MS traces retain the unchanged conservative core caller and strong roster-support fence. For an independently supplied event-list time only, a second manual-review gate may retain an exact, resolved MS760 local maximum above the 90th percentile of quiet-background maxima. It remains excluded from automatic calibration, time-difference estimation, QC, automatic Cell candidates, and model training.
- Project manifests and the roster-support audit record both strong and upper-decile review thresholds and the reason used for each retained review event.
- CAR-T Myr, Ctr–Myr–Bez, Ctr, and Bez raw inputs were exercised end to end with 700, 691, 625, and 1100 event-list rows respectively; every row resolved one-to-one without changing the core caller.

## Retained interaction behavior

- The shortcut reminder uses separate stable lines so it remains readable in the narrow annotation sidebar.
- Entering an MS760 time in UMAP now outlines matching points in red without changing the current zoom or pan.
- Double-clicking a coordinate-mapped MS760 peak in Track opens or restores the native UMAP window and outlines the same canonical event in red. The UMAP view remains unchanged; LIF and MS782 peaks are not treated as unique UMAP event identifiers.
- Peak calling, event-map binding, project schemas, CSV columns, and accepted/pending annotation semantics are unchanged. Existing projects keep their persisted calibration/time models; the event-complex rule applies when a new project computes its front calibration evidence.

## Compatibility

- Existing formal v0.4.0-v0.4.9 projects using the current adaptive two-tier LIF peak standard open from their manifest paths without migration or preprocessing reruns. Existing candidates, annotations, pending reviews, frozen models, and directory layouts remain intact.
- Opening an existing project never reruns the MS caller. The project continues to use its manifest-bound peak and event tables.
- Projects made with the retired fixed-threshold peak standard remain intentionally rejected before writes. Rebuild those projects in a new empty directory from the original LIF/MS/event-list inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than individual parquet, SQLite, or coordinate files.

## Validation

- Focused interaction tests cover accepted/pending shortcut guards and discoverability, the shared Track/UMAP palette, UMAP time lookup without view mutation, Track-to-UMAP event identity, and the native-window message handshake.
- The candidate is gated by the automated suite, JavaScript syntax checks, packaged scientific-binary provenance checks, ABI validation, the packaged runtime probe, and four real-data project-creation checks before formal publication.
- The formal tag workflow binds package names to the source `APP_VERSION`, verifies both platform archives against their SHA256 files, and publishes only after Windows x64 and macOS ARM64 jobs both pass.

## Data policy

The source repository and application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, exported annotations, credentials, or local absolute paths.
