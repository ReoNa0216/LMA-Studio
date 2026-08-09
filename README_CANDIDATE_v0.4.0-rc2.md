# LMA Studio v0.4.0-rc2 Candidate (superseded)

This candidate is retained as a historical record. Use `README_CANDIDATE_v0.4.0-rc3.md` and `docs/HSC1_v0.4.0-rc3_UAT.md` for current testing.

This is a user-acceptance candidate, not a formal Release.

## User-visible changes

- New projects use one automatic LIF peak-recognition standard. The UI no longer asks users to choose an implementation version.
- High-confidence peaks drive every automatic scientific step. Optional weak candidate peaks are display-only evidence for manual cell pairing and cannot train calibration, QC, time-difference, or candidate models.
- Front reference windows and later QC are independent. Front windows may use green-only, red-only, combined red/green, or sequential same-color populations.
- Dense LIF/MS matching preserves chronological order, so automatic reference lines cannot cross merely because a greedy nearest-neighbor match chose peaks out of sequence.
- A project can be created as an unconfirmed draft. Raw peak shapes remain viewable, while calibration and all dependent stages stay locked until every reference boundary is confirmed.
- The Track window width is editable from 0.25 to 15 minutes, with automatic peak-label thinning for dense windows.
- Buttons, field labels, tooltips, and status messages use user-facing scientific language; internal versions, hashes, strategy keys, and time-axis identifiers stay in project audit files.
- The main CSV remains a compact 16-column cell/later-QC table. Front reference records and technical audit metadata remain in the project database.

## Scientific boundary

The current peak-recognition standard keeps the original high-specificity calls as its high-confidence tier and adds locally normalized, morphology-filtered weak candidates. Weak candidates are deliberately excluded from every automatic evidence path, including reconstructed candidate IDs and accepted-reference refitting. A manually selected weak LIF peak may enter the main CSV only after the user explicitly pairs and accepts it as a cell event.

Projects created with the retired peak-recognition standard are rejected before any project write. LMA Studio explains that the original project is unchanged and asks the user to rebuild from original LIF, MS, and coordinate inputs in a new empty directory. The retired implementation remains in source only for offline scientific comparisons; it is not selectable in the desktop application.

## Candidate gates

- Full Python unit/integration suite.
- Main-page and UMAP JavaScript syntax checks.
- Strict synthetic weak-peak recall, false-discovery, and timing validation.
- Noise-only red-channel guard and read-only HSC1 G1/G2 audit.
- Windows PyInstaller runtime, DLL provenance/ABI, and simulated Internet-zone probes.
- Mouse-level HSC1 acceptance from a newly created project.
- No version tag or formal GitHub Release before Windows user acceptance.

Use `docs/HSC1_v0.4.0-rc2_UAT.md` for the Windows acceptance walkthrough. Exact test/build evidence is recorded after the final `dist\LMAStudio` rebuild.

## Exact candidate evidence (2026-08-10)

- Python unit/integration suite: 228 passed; 1 platform-only test skipped.
- Main-page and UMAP JavaScript syntax, Python compilation, and `git diff --check`: passed.
- Read-only HSC1 audit: source SHA256 values unchanged. G1 produced 1,002 high-confidence plus 71 weak candidates; G2 produced 928 high-confidence plus 350 weak candidates. R1 produced one isolated threshold-edge call and R2 produced 17 isolated threshold-edge calls across 17 different minutes, with no structured red-channel peak train.
- Windows executable: `dist\LMAStudio\LMAStudio.exe`, 19,169,888 bytes, SHA256 `E50FE8F9C8B6E5694081923C90DE0FBD1861F29E3A2BDE582B07B3A6E2F88C93`.
- Windows runtime audit: seven core DLL hashes match the selected environment; 120 scientific binaries have no foreign source, missing bundle row, or hash mismatch; `pyexpat` and `libexpat` are both x64 with no missing names or ordinals.
- Packaged runtime probes passed both normally and with simulated Internet-zone markers.
- Retired-project regression used temporary copies of Batch03Test, CART_Exp1-3, CART_Exp2-1, and Young_HSC3. Every copy was rejected with HTTP 400 before loading; all file paths, file hashes, sizes, and file modification times remained unchanged, and the originals remained unchanged. Temporary copies were removed.
- No formal version tag or Release was created.
