# LMA Studio v0.4.0-rc3 Candidate

This is a Windows user-acceptance candidate, not a formal Release.

## Changes from rc2

- Dense track controls and stage tabs now use compact scientific labels: `Calibration`, `MS Δt`, `Events / QC`, `Start`, `Window`, `Time`, `Y`, `Labels`, and `Weak peaks`.
- Weak LIF markers have a larger transparent hit target. Outside Events they explain that they are event-annotation only; inside `Events / QC → Cell pair → Select peaks` they can be selected, visibly highlighted, paired to MS760, and saved.
- Post-run QC choices are `Off`, `QC signature`, and `Scheduled windows`, with a short scenario-specific explanation below the control.
- A channel's QC-anchor and cell-pair roles remain independent and may both be enabled. The new-project UI explicitly explains the CART-style G2+R1 front-reference case.
- Confirming the last front-reference boundary now switches directly to `Calibration + Aligned`, refreshes the window, and reports that QC-anchor candidates were generated. This fixes the misleading no-lines state caused by remaining in Raw mode.
- A front QC relation that straddles a Track-window boundary now belongs to the window containing its MS760 event, while its LIF anchor may remain in the displayed ±0.08 min context. This removes the boundary blind spot without duplicating the relation in the adjacent window; such relations remain excluded from whole-window batch acceptance.
- Stale candidate IDs now produce a refresh/reselect instruction instead of being misreported as project-format damage.
- `Events / QC → Cell pair → Select peaks` now unlocks every eligible core LIF peak in the main window, including peaks that did not enter the automatic pending list. Switching selection mode repaints the markers immediately.
- A saved Cell pair that straddles a Track-window boundary is now owned by the window containing its MS760 event; the LIF endpoint may remain in the loaded ±0.08 min context. Saving from the adjacent window automatically moves the view to the owning window instead of making the accepted relation appear to vanish.
- Pale MS markers outside the event-coordinate whitelist remain non-selectable but now expose an explicit hover/click explanation. MS markers are not classified as weak; larger red-rimmed MS circles denote collision/quality risk.
- The narrow MS-offset action is now the compact, single-line `Estimate MS Δt` button.

## Scientific boundary

This candidate does not change peak detection, parquet schemas, project schemas, matching tolerances, or CSV columns. High-confidence peaks remain the only automatic evidence. Weak peaks remain manual cell-pair evidence only and cannot enter automatic calibration, QC, delta estimation, candidate generation, or model fitting.

Existing current-standard HSC1 projects do not require rebuilding. For UAT, first make a complete project copy and open only that copy. Projects made with the retired peak-recognition standard remain rejected before writes and must be rebuilt from original inputs in a new empty directory.

## Release boundary

- Windows source/unit/integration, packaged-runtime, DLL provenance, and UAT checks must pass.
- macOS ARM64 is an Actions candidate artifact only.
- Do not create a version tag or formal GitHub Release before Windows user acceptance.

Use `docs/HSC1_v0.4.0-rc3_UAT.md` for the current walkthrough. The exact Windows build evidence is recorded below.

## Windows build evidence (2026-08-10)

- Source commit used for the build: `8789a9cfda28cc56c1f7ef8fdaf5ea078b743040`.
- Automated suite: 242 tests passed; 1 POSIX-only test skipped on Windows.
- EXE: `dist\LMAStudio\LMAStudio.exe`, 19,173,038 bytes.
- EXE SHA256: `1F59C943678857A7E76DD47115D7CE2449CD0940F44BE89F412C9CEF6DC7060D`.
- Bundle audit: 120 scientific binaries checked; no foreign source, missing row, or hash mismatch. The packaged `pyexpat`/`libexpat` ABI and all seven guarded runtime DLL hashes matched the selected build environment.
- Normal and simulated downloaded-file runtime probes passed.
- Packaged HSC1 read-only regression used a temporary complete project copy and reported two 6.002/6.017 min boundary candidates, one 24–26.5 min Cell candidate, no post-run QC candidates, `ProjectStable=True`, `OriginalProjectStable=True`, `HscSourceStable=True`, `SmokeProcessExited=True`, plus the exact 16-column CSV header.
- A packaged boundary regression loaded the accepted MS760 26.513 min / G1 26.902 min Cell pair from a complete temporary HSC1 copy. It appeared exactly once in its MS-owned window, stayed absent from the adjacent window, and the packaged UI contained the automatic saved-relation focus behavior; the original HSC1 tree remained byte-for-byte unchanged.
- A separate packaged write exercise accepted both boundary relations through the real `/api/manual-triplet` path on another temporary copy, reported `CopyWriteIsolated=True`, and again proved the original HSC1 project and `HSC1_data` unchanged before deleting the copy.
- Retired-project rejection regression used temporary copies of Batch03Test, CART_Exp1-3, CART_Exp2-1, and Young_HSC3; every copy was rejected before writes and all originals remained unchanged.
