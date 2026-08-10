# LMA Studio v0.4.0 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.0-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.0-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## Highlights

- New projects use one adaptive two-tier LIF peak standard. Core peaks drive automatic calibration, time-difference estimation, QC, candidate generation, and model fitting; weak peaks remain optional evidence for manual Cell pair annotation only.
- Two to four LIF channels can independently serve as front `QC anchor`, Events-stage `Cell pair`, both roles, or neither. Channels sharing one physical acquisition clock also share one fitted translation.
- Ordered front reference windows support green-only, red-only, or combined evidence. Suggested boundaries remain project-level drafts until the user confirms them.
- Post-run QC is independent of front calibration and can be `Off`, `QC signature`, or `Scheduled windows`.
- Dense calibration and QC evidence uses deterministic order-preserving matching, preventing crossed peak assignments while preserving explicit ambiguity review.
- Downstream delta, event candidates, and writes require the current frozen time model. Configuration changes invalidate dependent results without silently deleting manual history.
- Track and UMAP remain synchronized. UMAP omits the QC legend when no active event is QC and can locate points by the MS760 time shown in Track, using a user-editable tolerance and red outlines.
- Saved Cell pairs crossing a Track window boundary appear in exactly one window that can draw the complete relation, including 1-minute windows.
- The main CSV is a compact 16-column full event roster. Unannotated events use `Type=unknown`; detailed audit and model metadata remain in SQLite.
- Projects made with the retired peak-recognition standard are rejected before writes and must be rebuilt in a new empty directory from original inputs.

## Data Policy

The application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical event-map CSV files, parquet tables, author CSV files, h5ad files, or exported annotations.

## Validation

- The complete automated suite, JavaScript syntax checks, packaging syntax checks, and bundled-runtime probes pass on the release source.
- Windows validation uses a complete temporary HSC1 project copy. The original HSC1 project and `HSC1_data` remain unchanged.
- The HSC1 regression verifies the 49.001-minute cross-window relation, MS760-time UMAP lookup, disabled post-run QC, the exact 16-column CSV contract, and `unknown` rows.
- Batch03Test, CART_Exp1-3, CART_Exp2-1, and Young_HSC3 are tested only as complete temporary copies; retired projects are rejected before any write and originals remain unchanged.
- macOS ARM64 validation runs the full test suite, builds the `.app`, validates the bundle and plist, checks ARM64 architecture, applies an ad-hoc signature, and runs a command-line startup smoke test.
