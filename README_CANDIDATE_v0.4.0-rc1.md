# LMA Studio v0.4.0-rc1 Candidate (superseded)

This earlier candidate is retained only as a historical record. Do not use it for current HSC1 acceptance. Use `README_CANDIDATE_v0.4.0-rc3.md` and `docs/HSC1_v0.4.0-rc3_UAT.md` instead.

This is a user-acceptance candidate, not a formal Release.

Key changes:

- Split front `calibration_protocol` from post-QC `signature / scheduled_windows / disabled` policy.
- Added Green-only, Red-only, Red+Green, and ordered segmented references.
- Added read-only raw peak-shape window suggestions that always require explicit user confirmation.
- Added shared physical-axis calibration, including sequential HSC1 G1/G2 on one `green_axis`.
- Added generic unlabeled local delta, post-QC strategy binding, and same-MS cross-channel arbitration.
- Preserved v0.3 G2+R1 projects through a non-mutating compatibility adapter.
- Replaced fixed preprocessing phase semantics with project-driven boundaries.
- Reduced the main accepted-annotation CSV to 16 downstream-oriented columns.

Validation gates:

- Python unit/integration suite must pass.
- Windows PyInstaller runtime and MOTW probes must pass.
- Existing-project tests must run only on temporary copies and prove protected data unchanged.
- macOS ARM64 must be built through manual `workflow_dispatch` and uploaded only as an Actions artifact.
- Do not create a tag or formal GitHub Release before Windows user acceptance.

Local candidate evidence:

- 153 Python unit/integration tests passed on Windows; one POSIX-only lock test was skipped by platform design.
- Main-page and UMAP inline JavaScript passed syntax checks.
- The packaged Windows EXE passed runtime and Mark-of-the-Web probes.
- Packaged copy regressions passed for `Batch03Test`, `CART_Exp1-3`, `CART_Exp2-1`, and `Young_HSC3`, with original project snapshots unchanged.
- An isolated real HSC1 project produced schema 3/layout 4, 1,325 LIF peaks, 5,387 MS events, a 971-row coordinate whitelist, sequential G1/G2 Green-only segments, a single shared `green_axis`, 24 min annotation start, disabled post-QC, and the exact compact CSV header. `HSC1_data` remained unchanged.

Use `docs/HSC1_v0.4.0-rc1_UAT.md` for mouse-level Windows acceptance. The remaining release gate is user acceptance; this candidate must not be tagged.
