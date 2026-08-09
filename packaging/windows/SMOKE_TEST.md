# Windows v0.4 Candidate Smoke Test

Goal: verify that the packaged LMA Studio candidate can create and open projects, run the split calibration/post-QC workflow, preserve legacy projects, and export the compact downstream CSV without requiring a user Python installation.

## Safety and release boundary

- This is a candidate acceptance test, not a formal Release procedure.
- Do not create or push a version tag before Windows user acceptance.
- Open existing projects only through temporary copies. Never point a write test at `HSC1_data` or an existing user project.
- HSC1 raw files may be selected only as read-only external references; save the generated project in a separate new directory.
- Keep the entire `dist/LMAStudio` directory together.

## 1. Build

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/build_windows.ps1
```

Expected:

- The full automated suite passes.
- PyInstaller creates `dist/LMAStudio/LMAStudio.exe`.
- The normal packaged-runtime probe and simulated Internet-zone/MOTW probe both pass.
- `dist/LMAStudio/_internal/scripts/v3` contains `project_protocol.py`, `run_v3_01_lif_trace_physical_qc.py`, and `run_v3_02_ms_event_calling.py`.

## 2. First startup

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/run_exe.ps1
```

Expected:

- A native window titled `LMA Studio` opens without an external browser or console.
- The initialization page offers `新建项目` and `打开项目`.
- No previous project is restored automatically.
- A second launch reports that LMA Studio is already running.
- Closing the main window terminates its loopback server and process.

## 3. Create a v0.4 project

Create only in a new empty project directory. Configure:

- Two to four distinct LIF raw files and one MS raw file.
- One event-coordinate CSV in which `scan_start_time`, `UMAP1`, and `UMAP2` can be found; unrelated columns are allowed.
- For every LIF channel: channel name, detector, scientific identity/sample label, and cell-annotation role. The shared physical time axis is assigned automatically from the detector.
- One or more ordered front calibration segments, each with a population label, reference channel set, editable `start_min / end_min`, and explicit `边界已确认` checkbox.
- A project-specific event annotation start and unlabeled-delta seed window.
- An independent later QC policy: `disabled`, `signature` plus channels, or `scheduled_windows` plus ordered non-overlapping windows and channels.

Exercise small fixtures for Green-only, Red-only, Red+Green, and sequential same-axis references. Also exercise two- and four-channel layouts.

Click `分析已选 LIF 并建议窗口`. Expected:

- The status explicitly says that the scan is read-only.
- Suggested boundaries come from the selected raw peaks.
- Every suggested or edited segment remains unconfirmed until the user checks `边界已确认`.
- Missing, ambiguous, wrong-order, or overlapping evidence is surfaced and cannot be silently confirmed.
- Numeric suggested windows may be saved as an unconfirmed project draft. The draft opens in raw front-track mode; front alignment, local delta, freezing, and event annotation remain locked.

After creation, verify:

- `lifms_project.json` records project schema 3 and acquisition layout 4.
- The manifest contains `calibration_protocol`, `post_qc_strategy`, channel detector/time-axis roles, and project-specific annotation settings.
- `results/tables/v3/00_project_protocol.json` records draft windows explicitly as `calibration_draft:*`; after every segment is confirmed it records the confirmed boundaries used by both preprocessing stages.
- `data/interim/lma/cell_event_umap.csv` contains exactly `ms_event_id,scan_id,scan_start_time,UMAP1,UMAP2`.
- Source `Type`, `leiden`, `CellNumber`, h5ad labels, and author/manual CSV content do not enter candidate generation.
- Intermediate parquet tables and `annotation_app/annotations/annotation.sqlite` are created only under the new project.

For full HSC1 acceptance, follow `docs/HSC1_v0.4.0-rc1_UAT.md`. Do not run the 8 GB MS import as part of a routine package smoke test.

## 4. Open legacy projects on copies

Use a temporary copy of a v0.3 G2+R1 project.

Expected:

- The project opens through the compatibility adapter.
- Existing `qc_anchor_channels` retain their historical v0.3 meaning.
- Opening does not rewrite the legacy manifest, accepted/rejected annotations, audits, or parquet inputs.
- Existing labels and review semantics remain visible.
- A project without an event map stays in its documented legacy workflow until a map is explicitly attached.
- Attaching a map preserves annotations and time models and rejects a map missing an already accepted post-start event.

Do not interpret historical fixed boundaries as defaults for a new project.

## 5. Front segmented calibration

Open `前段参考校准`.

Expected:

- Candidates are grouped by configured calibration segment, not one global `qc_anchor_channels` interval.
- Green-only and Red-only segments do not require the absent detector.
- Red+Green segments display the configured cross-detector evidence.
- Sequential G1/G2 segments sharing `green_axis` contribute to one Green-axis translation without requiring simultaneous peaks.
- Manual front anchors require an explicit segment and obey its channel policy.
- Accepted-anchor preview reports included evidence, conflicts/outliers, old/new per-axis shifts, and evidence sufficiency.
- `应用 QC 对齐（按物理轴）` applies at most one translation per physical time axis and invalidates the dependent delta/time model.

Wrong-order, overlapping, missing, or unconfirmed segments must block saving or preprocessing with a readable error.

## 6. Unlabeled later delta and freeze gate

Open `无标签后段 delta`.

Expected:

- The recommendation uses generic unlabeled LIF/MS peak topology and does not depend on QC identity labels.
- The user can inspect and edit the delta before freezing.
- Before freezing the current time model, third-stage acceptance and direct backend writes are blocked.
- Freezing unlocks only results bound to the current layout, protocol, front alignment, annotation start, and delta settings.

After freezing, edit a confirmed segment boundary, annotation start, or delta dependency:

- A clear confirmation warning appears.
- Confirming invalidates the old frozen model, dependent delta, and third-stage candidates.
- Existing manual annotation/audit history remains stored and is not silently deleted or reused as current output.
- Recompute and freeze again before continuing.

## 7. Event annotation and independent post-QC

Open `事件标注 / QC 巡检` and verify each policy:

- `disabled`: no later QC candidates or manual QC writes; only cell candidates are shown.
- `signature`: later QC candidates use only the configured signature channels.
- `scheduled_windows`: QC candidates occur only in declared windows with their configured channels, including a declared pre-annotation window when applicable.

For all policies:

- QC and cell writes require the current frozen model and canonical event-map whitelist.
- Third-stage candidates are accepted one at a time; there is no whole-window batch acceptance.
- One MS event cannot simultaneously hold active QC and cell semantics.
- If G1 and G2 both match one MS event, both candidates show cross-channel ambiguity and require explicit selection of one channel.
- Changing only `post_qc_strategy` preserves old reviews for audit, makes old strategy-bound QC rows inactive/non-exportable, and does not alter front calibration.

## 8. Track and UMAP synchronization

On a project with a canonical event map:

- `UMAP` opens one resizable window; repeated clicks restore it rather than creating duplicates.
- Unknown points are gray, accepted QC is black, accepted cells use the selected LIF channel color, and conflicts are explicit.
- Accept/revoke in Track updates UMAP without reloading.
- Clicking a UMAP point opens the same canonical `ms_event_id` in its containing 2.5-minute Track window.
- Project/map identity prevents events from a prior project leaking into the current window.
- Closing the main window closes UMAP and releases the listener.

## 9. Compact CSV contract

Click `导出 Cell/QC 主 CSV`.

The header must be exactly these 16 columns, in this order:

```text
CellNumber,scan_Id,scan_start_time,TIC,PC(34:1)_mz,PC(34:1)_intensity,UMAP1,UMAP2,Type,annotation_kind,review_stage,LIF_channel,LIF_peak_id,MS_event_id,residual_sec,annotation_id
```

Verify:

- Only active accepted/exportable third-stage Cell and post-QC relations are present.
- Front calibration anchors remain in SQLite/audit history and never create blank `CellNumber` rows in the main CSV.
- `CellNumber` is the stable canonical event-map order identifier for mapped third-stage rows.
- Cell `Type` comes from the accepted LIF channel’s project identity; QC rows use `QC`.
- Source CSV `Type`, `leiden`, or author `CellNumber` values are never copied into the export.
- Core MS/LIF values and identifiers are readable without decoding JSON payloads.
- Protocol/model hashes, ambiguity alternatives, payloads, and audit metadata remain in SQLite.
- The downloaded file and project-local copy under `annotation_app/annotations/exports` match.

## 10. Existing-project copy regression

Close every running LMA Studio window, then run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/regression_existing_projects.ps1
```

Expected:

- The script refuses a copy root outside system temp or without the `LMAStudioProjectRegression_*` prefix.
- It copies `Batch03Test`, `CART_Exp1-3`, `CART_Exp2-1`, and `Young_HSC3` before opening them.
- Original-project protected snapshots match before and after the run.
- Copy manifest, SQLite annotations/audits/models, and parquet hashes satisfy compatibility checks.
- The packaged export has the exact 16-column header above.
- The result reports `DataStable=True`, `OriginalStable=True`, and `ClosedCleanly=True`.
- The temporary regression root is removed even after failure.

After a real HSC1 candidate project has been created outside `HSC1_data`, run the optional packaged smoke on another temporary copy:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/regression_hsc1_packaged.ps1 `
  -ProjectDir "E:\path\to\HSC1_v0.4.0_rc1_candidate" `
  -HscDataDir "E:\path\to\HSC1_data"
```

It verifies schema 3/layout 4, G1/G2 on one `green_axis`, 24 min, disabled post-QC, Track-window APIs, the 16-column export, unchanged project/source snapshots, and safe cleanup under `%TEMP%\LMAStudioProjectRegression_*`.

## 11. Package boundary

Inspect `dist/LMAStudio` and any candidate archive. They must not contain user project directories, annotation databases or exports, raw LIF/MS inputs, source/canonical event maps, project parquet tables, h5ad files, or author/manual outputs.

A manual GitHub Actions `workflow_dispatch` may upload a candidate artifact. Do not create a tag or formal GitHub Release before Windows user acceptance.
