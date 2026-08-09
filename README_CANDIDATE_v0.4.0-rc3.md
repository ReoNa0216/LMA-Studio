# LMA Studio v0.4.0-rc3 Candidate

This is a Windows user-acceptance candidate, not a formal Release.

## Changes from rc2

- Dense track controls and stage tabs now use compact scientific labels: `Calibration`, `MS Δt`, `Events / QC`, `Start`, `Window`, `Time`, `Y`, `Labels`, and `Weak peaks`.
- Weak LIF markers have a larger transparent hit target. Outside Events they explain that they are event-annotation only; inside `Events / QC → Cell pair → Select peaks` they can be selected, visibly highlighted, paired to MS760, and saved.
- Post-run QC choices are `Off`, `QC signature`, and `Scheduled windows`, with a short scenario-specific explanation below the control.
- A channel's QC-anchor and cell-pair roles remain independent and may both be enabled. The new-project UI explicitly explains the CART-style G2+R1 front-reference case.
- Confirming the last front-reference boundary now switches directly to `Calibration + Aligned`, refreshes the window, and reports that QC-anchor candidates were generated. This fixes the misleading no-lines state caused by remaining in Raw mode.

## Scientific boundary

This candidate does not change peak detection, parquet schemas, project schemas, matching tolerances, or CSV columns. High-confidence peaks remain the only automatic evidence. Weak peaks remain manual cell-pair evidence only and cannot enter automatic calibration, QC, delta estimation, candidate generation, or model fitting.

Existing current-standard HSC1 projects do not require rebuilding. For UAT, first make a complete project copy and open only that copy. Projects made with the retired peak-recognition standard remain rejected before writes and must be rebuilt from original inputs in a new empty directory.

## Release boundary

- Windows source/unit/integration, packaged-runtime, DLL provenance, and UAT checks must pass.
- macOS ARM64 is an Actions candidate artifact only.
- Do not create a version tag or formal GitHub Release before Windows user acceptance.

Use `docs/HSC1_v0.4.0-rc3_UAT.md` for the current walkthrough. Exact Windows EXE size/SHA and final test count are recorded after rebuilding `dist\LMAStudio`.
