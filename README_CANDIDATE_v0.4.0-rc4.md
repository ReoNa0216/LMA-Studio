# LMA Studio v0.4.0-rc4 Candidate (superseded by v0.4.0)

Use `README_RELEASE.md` and `docs/HSC1_v0.4.0_UAT.md` for the final release. The remainder of this file preserves rc4 evidence only.

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

The matching historical walkthrough is `docs/HSC1_v0.4.0-rc4_UAT.md`; current testing uses the final v0.4.0 guide noted above.

## Windows build evidence

Verified on 2026-08-10:

- Source commit used for the build: `237c50729f546f9faf25310ffdec5993c9afb54c`.
- Automated suite: 280 tests passed; 1 POSIX-only test skipped on Windows. Main-page and UMAP JavaScript syntax checks also passed.
- EXE: `dist\LMAStudio\LMAStudio.exe`, 19,177,953 bytes.
- EXE SHA256: `61C8BBE6AA64F95D165B40DF29EEFCCB41218A08229ECD1126AF83EE2C8CB495`.
- Normal and simulated downloaded-file runtime probes passed.
- Bundle audit checked 120 scientific binaries: no foreign source, missing bundle row, or hash mismatch. The packaged `pyexpat`/`libexpat` pair had no missing name or ordinal.
- Packaged HSC1 regression used a complete temporary project copy. It reported schema 3/layout 4, G1/G2 on one Green axis, 24 min start, Post-run QC `Off`, 971 CSV rows, 10 `unknown`, 0 QC, and the exact 16-column header.
- The packaged `Save pair` write took 0.140 s and the following Track refresh 0.589 s (0.729 s total) on the annotation-rich HSC1 copy.
- Separate source-level HSC1 copy probes measured accept+refresh at 0.525 s and reject+refresh at 0.486 s.
- Both packaged HSC1 runs reported `OriginalProjectStable=True` and `HscSourceStable=True`; their disposable copies were removed.
- Retired-project checks used complete temporary copies of Batch03Test, CART_Exp1-3, CART_Exp2-1, and Young_HSC3. All were rejected before writes and all protected originals remained unchanged.
- macOS ARM64 Actions run [`31376412190`](https://github.com/ReoNa0216/LMA-Studio/actions/runs/31376412190) passed on candidate commit `ff6df3b90fbb5490576b8a9e4621c580e934374b`. Artifact `lma-studio-macos-arm64` (ID `9058136138`, 88,573,560 bytes) was uploaded; the Windows and publish jobs were skipped.
- No `v0.4` tag or formal Release was created.
