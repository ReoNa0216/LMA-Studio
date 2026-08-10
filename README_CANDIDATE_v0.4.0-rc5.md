# LMA Studio v0.4.0-rc5 Candidate (superseded by v0.4.0)

This candidate used the wrong UMAP lookup field. The final v0.4.0 release uses MS760 time from the event-coordinate table. Use `README_RELEASE.md` and `docs/HSC1_v0.4.0_UAT.md`; the remainder of this file is historical evidence only.

This is a user-acceptance candidate, not a formal Release.

## Changes from rc4

- A saved Cell pair that crosses a Track window boundary now appears in exactly one window that can draw both its LIF and MS endpoints. The fix covers 0.25, 0.5, 1.0, and 2.5 min windows in both directions.
- Saving such a pair no longer jumps to an adjacent window that cannot display the complete relation.
- The UMAP window can locate events by their per-scan PC(34:1) m/z with a user-editable tolerance. All matches receive a red outline and the view zooms to them; the query is read-only.
- Existing annotations, the 16-column CSV contract, frozen time models, peak tables, and project schemas are unchanged.

## Scientific and migration boundary

Existing current-standard HSC1 projects do not need rebuilding or relabeling. UAT must use a complete project copy; original projects and `HSC1_data` remain untouched.

The m/z lookup uses `pc34_760_mz_at_max_intensity` from the MS scan bound to each event-map point. It does not infer an ion identity and it does not change any annotation.

## Release boundary

- Validate the Windows candidate in `dist\LMAStudio`.
- Produce a matching macOS ARM64 Actions candidate artifact.
- Do not create a version tag or formal GitHub Release before Windows user acceptance.

Use `docs/HSC1_v0.4.0-rc5_UAT.md` for the current walkthrough.

## Candidate evidence

- Candidate source commit: `dba7d2a742f1cf94844dd1c70afca2be0a75c530`.
- Source suite: 285 tests passed; 1 POSIX-only test skipped on Windows.
- Main-page and UMAP JavaScript syntax checks passed.
- A complete temporary HSC1 project copy showed `manual_cell:b2dd9e046d` zero times in 48–49 min and exactly once in 49–50 min at 1.00 min width.
- The same HSC1 copy exposed PC(34:1) m/z for all 971 event-map points; `MS_pc34_primary_000770` mapped to m/z `760.591882537` and is unique at the default ±0.0001 Da tolerance.
- The packaged Windows HSC1-copy regression passed: the 49.001 relation appeared in exactly one complete window, 971/971 UMAP points carried m/z, the lookup returned one point, the exported CSV had the exact 16-column contract and 971 rows, 6 rows remained `unknown`, and no QC rows were exported.
- Packaged retired-project regression copied Batch03Test, CART_Exp1-3, CART_Exp2-1, and Young_HSC3 to `%TEMP%`; all four were rejected before writes with the current-standard migration message, all original tree snapshots remained unchanged, and all temporary copies were removed.
- The protected original HSC1 tree SHA256 aggregate remained `ed6876708488b1b7c2cfd0034a1793071224ec1077969db29e70a36ee2544193` before and after the copy test.
- Windows candidate: `dist\LMAStudio\LMAStudio.exe`, 19,181,254 bytes, SHA256 `8F7982F8CAF9A04DEF2B1C65E1063485A2BA91BD0308B7B3ED1A4846DFBD7DF6`; runtime and bundled dependency checks passed.
- macOS ARM64 candidate: GitHub Actions run `31379178043` succeeded from the same source commit; artifact `lma-studio-macos-arm64` (ID `9059216839`, 88,577,411 bytes) is available and not expired. The Release publishing job was skipped.
- No `v0.4` tag or formal GitHub Release exists. Formal publication remains gated on Windows user acceptance.
