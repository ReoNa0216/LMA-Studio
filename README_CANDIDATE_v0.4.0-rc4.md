# LMA Studio v0.4.0-rc4 Candidate

This is a Windows user-acceptance candidate, not a formal Release.

## Changes from rc3

- Candidate accept/reject and manual `Save pair` reuse one request-local read snapshot. Runtime no longer scales SQLite reads with the number of historical annotations.
- Pending cross-channel Cell conflicts are hidden from the normal plot and review list. `Show conflicts` exposes one grouped card per MS event for explicit channel arbitration.
- The UMAP legend shows `QC` only when the current project has at least one active post-run QC event. Historical QC rows under an `Off` policy remain audit history and do not color the current map.
- The compact 16-column CSV is now a complete event roster. Unannotated event-map rows remain present as `Type=unknown`; accepted Cell or current QC relations replace that classification without changing `CellNumber`.
- Front `QC anchor` evidence remains SQLite/audit-only and never creates calibration rows in the main CSV.
- Sidebar action buttons wrap within their cards, and review actions show immediate busy feedback.

## Scientific and migration boundary

This candidate does not change peak detection, parquet schemas, project schemas, matching tolerances, frozen time models, or existing annotations. Existing current-standard HSC1 projects do not need rebuilding or relabeling. UAT must use a complete project copy; original projects and `HSC1_data` remain untouched.

Projects made with the retired peak-recognition standard remain rejected before writes and must be rebuilt from original inputs in a new empty directory.

## Release boundary

- Windows source/unit/integration, packaged-runtime, DLL provenance, and project-copy checks must pass.
- macOS ARM64 remains an Actions candidate artifact.
- Do not create a version tag or formal GitHub Release before Windows user acceptance.

Use `docs/HSC1_v0.4.0-rc4_UAT.md` for the current walkthrough.

## Windows build evidence

Build evidence is populated after the rc4 source commit and reproducible Windows packaging run.
