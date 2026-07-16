# LMA Studio v0.2.0 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.2.0-windows-x64.zip`: fully validated Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.2.0-macos-arm64.zip`: Apple Silicon candidate built and structurally verified on GitHub Actions. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**. A physical Mac GUI acceptance test has not yet been performed.

## Highlights

- Native application window backed by pywebview instead of opening the system browser.
- Initialization page always starts without silently reopening the last project.
- Directory-based projects with `lifms_project.json` and external raw-input references.
- Configurable three-channel LIF layout and a dynamic two-to-three-channel QC anchor set.
- Axis-aware QC calibration, including shared green-axis timing for G1/G2.
- Reviewed QC relations no longer reappear as overlapping pending candidates.
- Project-level acquisition and phase configuration with frozen-model invalidation safeguards.
- Four workflow stages: QC calibration, local post-QC MS shift calibration, QC survey, and cell annotation.
- Accepted-annotation CSV exports named with the project and timestamp.

## Data Policy

The application packages contain no user projects, raw LIF/MS files, SQLite databases, author CSV files, h5ad files, or exported annotations.

## Validation

Windows validation completed locally:

- 79 automated tests.
- Clean PyInstaller x64 build and native-window startup at the initialization page.
- Read-only open regressions for `Batch03Test`, `CART_Exp1-3`, `CART_Exp2-1`, and `Young_HSC3`.
- Annotation/database/project-file hashes remained unchanged during regression checks.

macOS ARM64 validation performed in GitHub Actions:

- Full automated test suite on an Apple Silicon runner.
- PyInstaller `.app` build, bundle/plist validation, ad-hoc code-signature verification, ARM64 executable inspection, and command-line startup smoke test.
