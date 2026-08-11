# LMA Studio v0.4.3 Release Notes

LMA Studio is a local desktop application for project-based, human-assisted LIF-MS annotation review.

## Downloads

- `LMA-Studio-v0.4.3-windows-x64.zip`: Windows x64 build. Keep the extracted folder together and run `LMAStudio.exe`.
- `LMA-Studio-v0.4.3-macos-arm64.zip`: Apple Silicon build. Open `LMA Studio.app`.
- Matching `.sha256` files are provided for integrity verification.

The macOS package is ad-hoc signed, not Apple Developer ID signed or notarized. On first launch, macOS may require Control-clicking the app and choosing **Open**.

## What changed in v0.4.3

- The macOS `UMAP` action now reliably creates a second native Cocoa/WebKit window through Python and pywebview. It never falls back to Chrome or another external browser.
- The toolbar waits for the documented `pywebviewready` event before calling the Python bridge. Repeated clicks restore the existing UMAP window; closing it and clicking again creates a fresh native window.
- pywebview 6.2.1 creates bridge functions dynamically. LMA Studio therefore gives only the exact native main document a process-random capability URL and the narrow CSP allowance needed to install that bridge. Ordinary browser pages and all API responses keep the strict CSP.
- Python accepts only the current loopback server's exact `/umap` URL before creating the child window. The capability and server address are runtime-only and are never written into a project.
- Manual `Cell pair` relations can still be saved as pending with `Save pending`, then accepted or rejected later. No review, matching, export, or project-storage semantics changed in this patch.

## Compatibility

- Project schema, peak tables, time models, annotation SQLite schema, portable directory layout, UMAP coordinate map, and the compact 16-column CSV contract are unchanged from v0.4.2.
- Existing formal v0.4.0-v0.4.2 projects that use the current peak-recognition standard open in place without migration or preprocessing reruns. Existing annotations and frozen models remain intact.
- Projects made with the retired peak-recognition standard remain intentionally rejected before writes. Rebuild those projects in a new empty directory from the original LIF/MS/coordinate inputs.
- Close LMA Studio before copying or compressing a project, and share the complete project root rather than individual parquet, SQLite, or coordinate files.

## Validation

- The v0.4.3 release candidate passed mouse-level testing on a real Apple Silicon Mac: first open, repeated-click reuse, close, and reopen all used the independent native UMAP window while the main annotation window stayed usable.
- Automated tests cover bridge readiness, URL validation, native-window reuse/recreation, strict-CSP isolation, project compatibility, annotation behavior, CSV output, and packaging boundaries.
- The formal tag workflow rebuilds both Windows x64 and macOS ARM64 packages from the same source commit and publishes the Release only after both packaged-runtime gates pass.

## Data policy

The application packages contain no user projects, raw LIF/MS files, SQLite databases, source or canonical UMAP CSV files, parquet tables, author CSV files, h5ad files, or exported annotations.
