# Windows v0.4.2 Release Smoke Test

Goal: verify that the packaged LMA Studio release can create and open current-standard projects, run the split calibration/post-QC workflow, reject retired peak-table projects without writes, and export the compact downstream CSV without requiring a user Python installation.

## Safety and release boundary

- Run this smoke test against the exact commit intended for the formal Release.
- Formal publication remains tag-triggered and must follow the automated release gates.
- Open existing projects only through temporary copies. Never point a write test at `HSC1_data` or an existing user project.
- `HSC1_data` raw files may be selected only as read-only external references; save the generated project in a separate new directory. The existing current-standard project is named `Lin-_LSK`.
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
- `dist/LMAStudio/_internal/scripts/v3` contains `lif_peak_detection.py`, `project_protocol.py`, `project_storage.py`, `run_v3_01_lif_trace_physical_qc.py`, and `run_v3_02_ms_event_calling.py`.

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

## 3. Create a v0.4.2 project

Create only in a new empty project directory. Configure:

- Two to four distinct LIF raw files and one MS raw file.
- One automatically configured adaptive two-tier LIF peak standard. The UI must not expose detector version choices or internal configuration keys.
- One event-coordinate CSV in which `scan_start_time`, `UMAP1`, and `UMAP2` can be found; unrelated columns are allowed.
- For every LIF channel: channel name, signal color, scientific identity/sample label, and cell-annotation role. The shared acquisition-time group is assigned automatically from the signal color.
- One or more ordered front calibration segments, each with a population label, reference channel set, editable `start_min / end_min`, and explicit `边界已确认` checkbox.
- A project-specific event annotation start and unlabeled-delta seed window.
- An independent later QC policy: `Off`, `QC signature` plus channels, or `Scheduled windows` plus ordered non-overlapping windows and channels.
- A channel may be both a front `QC anchor` and an Events-stage `Cell pair` channel; these roles are not mutually exclusive.

Exercise small fixtures for Green-only, Red-only, Red+Green, and sequential same-axis references. Also exercise two- and four-channel layouts.

Click `分析已选 LIF 并建议窗口`. Expected:

- The status explicitly says that the scan is read-only.
- Suggested boundaries come from the selected raw peaks.
- Every suggested or edited segment remains unconfirmed until the user checks `边界已确认`.
- Missing, ambiguous, wrong-order, or overlapping evidence is surfaced and cannot be silently confirmed.
- Numeric suggested windows may be saved as an unconfirmed project draft. The draft opens in raw front-track mode; front alignment, local delta, freezing, and event annotation remain locked.

After creation, verify:

- `lifms_project.json` records project schema 3 and acquisition layout 4.
- The manifest contains `lif_peak_detection` plus its scientific hash, `calibration_protocol`, `post_qc_strategy`, channel detector/time-axis roles, and project-specific annotation settings.
- `provenance/project_protocol.json` records draft windows explicitly; after every segment is confirmed it records the confirmed boundaries used by both preprocessing stages.
- `data/cell_event_map.csv` contains exactly `ms_event_id,scan_id,scan_start_time,UMAP1,UMAP2`.
- Source `Type`, `leiden`, `CellNumber`, h5ad labels, and author/manual CSV content do not enter candidate generation.
- The four runtime parquet tables are created directly under `data/`; `annotations/annotation.sqlite` and `annotations/exports/` are created only under the new project.
- `provenance/` contains the input manifest, protocol, log, and import report; `diagnostics/lif` and `diagnostics/ms` contain optional review outputs.
- The new project tree contains no pipeline-version directory and includes a root `README.md` explaining portability and sharing.

Follow `docs/Lin-_LSK_v0.4.2_UAT.md`. Do not run the 8 GB MS import as part of a routine package smoke test.

## 4. Reject retired peak-standard projects without writes

Use a temporary copy of a project whose peak tables were created with the retired detector.

Expected:

- Opening is refused with a readable instruction to rebuild from original LIF/MS/coordinate inputs in a new empty directory.
- No manifest, annotation database, parquet table, timestamp, or other file-tree entry changes.
- The currently loaded project, if any, remains open.
- The dialog offers a clear route back to new-project creation without implying that annotations are migrated automatically.

Separately, exercise a current-standard fixture that uses the historical G2+R1 calibration/post-QC semantics. Its annotation meanings remain unchanged; detector retirement and calibration-protocol compatibility are independent boundaries.

Do not interpret historical fixed boundaries as defaults for a new project.

## 5. Front segmented calibration

Open `Calibration`.

Expected:

- Candidates are grouped by configured calibration segment, not one global `qc_anchor_channels` interval.
- Dense candidates preserve chronological order; nearby peaks may remain ambiguous, but accepted proposals must never cross in time.
- Green-only and Red-only segments do not require the absent detector.
- Red+Green segments display the configured cross-detector evidence.
- Sequential G1/G2 segments sharing `green_axis` contribute to one Green-axis translation without requiring simultaneous peaks.
- Manual front anchors require an explicit segment and obey its channel policy.
- Confirming the last draft boundary automatically selects `Time = Aligned`, refreshes the window, and shows the generated QC-anchor connectors.
- Accepted-anchor preview reports included evidence, conflicts/outliers, old/new per-axis shifts, and evidence sufficiency.
- `应用 QC 对齐（按物理轴）` applies at most one translation per physical time axis and invalidates the dependent delta/time model.

Wrong-order, overlapping, missing, or unconfirmed segments must block saving or preprocessing with a readable error.

## 5a. LIF peak evidence boundary

On a newly built project:

- The legacy high-specificity calls are tagged `core`; automatic alignment, delta, and cell candidates use only core peaks.
- `Weak peaks` is off by default. Enabling it adds hollow/dashed weak markers without changing automatic candidates or the time model.
- Clicking one outside `Events / QC` shows `仅在事件标注段生效`. Inside `Events / QC`, `Cell pair` + `Select peaks` can select it through the enlarged hit target and `Save pair` can persist the manual relation.
- Weak evidence is learned only on a channel with enough core pulse evidence. Pure/no-signal channels must not be expanded into dense weak calls.
- A user-confirmed weak peak/MS event cell pair remains exportable, while any attempt to use weak evidence as a QC or delta training anchor is rejected clearly.
- A retired-standard project is rejected before any write and must be rebuilt in a new empty directory. There is no detector-version selector in the desktop UI.

## 6. Unlabeled later delta and freeze gate

Open `MS Δt`.

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

Open `Events / QC` and verify each policy:

- `Off`: no later QC candidates or manual QC writes; only cell candidates are shown.
- `QC signature`: later QC candidates use only the configured channels across the Events segment.
- `Scheduled windows`: QC candidates occur only in declared windows with their configured channels, including a declared pre-annotation window when applicable.

For all policies:

- QC and cell writes require the current frozen model and canonical event-map whitelist.
- Third-stage candidates are accepted one at a time; there is no whole-window batch acceptance.
- One MS event cannot simultaneously hold active QC and cell semantics.
- If G1 and G2 both match one MS event, the ambiguous candidates are hidden from the normal list and plot. `Show conflicts` exposes one grouped card for explicit selection of one channel.
- Changing only `post_qc_strategy` preserves old reviews for audit, makes old strategy-bound QC rows inactive/non-exportable, and does not alter front calibration.

## 8. Track and UMAP synchronization

On a project with a canonical event map:

- `UMAP` opens one resizable window; repeated clicks restore it rather than creating duplicates.
- Unknown points are gray and accepted cells use the selected LIF channel color. Accepted current QC is black, but the QC legend is omitted when its current count is zero; conflicts remain explicit.
- Accept/revoke in Track updates UMAP without reloading.
- Clicking a UMAP point opens the same canonical `ms_event_id` in its containing 2.5-minute Track window.
- Project/map identity prevents events from a prior project leaking into the current window.
- Closing the main window closes UMAP and releases the listener.
- In `配置 > UMAP coordinates`, import an alternate CSV for the same MS events. `Validate & switch` must preserve annotation counts and the frozen time model, refresh the open UMAP window, and make the next main CSV use the new coordinates.
- Rename or move the external CSV after a successful switch. UMAP must still open because the project owns a normalized internal copy rather than an external absolute path.
- A CSV that omits or adds an MS event must be rejected, and the previously active UMAP coordinates must remain unchanged.

## 9. Compact CSV contract

Click `导出 Cell/QC 主 CSV`.

The header must be exactly these 16 columns, in this order:

```text
CellNumber,scan_Id,scan_start_time,TIC,PC(34:1)_mz,PC(34:1)_intensity,UMAP1,UMAP2,Type,annotation_kind,review_stage,LIF_channel,LIF_peak_id,MS_event_id,residual_sec,annotation_id
```

Verify:

- Every canonical event-map row is present exactly once. Events without a current accepted Cell or post-QC relation use `Type=unknown` and blank annotation-specific fields.
- Front calibration anchors remain in SQLite/audit history and never create blank `CellNumber` rows in the main CSV.
- `CellNumber` is the stable canonical event-map order identifier for mapped third-stage rows.
- Cell `Type` comes from the accepted LIF channel’s project identity; QC rows use `QC`.
- When post-run QC is `Off`, historical QC rows remain in audit history but export as the current event classification (normally `unknown` unless labeled as a Cell), never as `QC`.
- Source CSV `Type`, `leiden`, or author `CellNumber` values are never copied into the export.
- Core MS/LIF values and identifiers are readable without decoding JSON payloads.
- Protocol/model hashes, ambiguity alternatives, payloads, and audit metadata remain in SQLite.
- The downloaded file and project-local copy under the manifest-selected export directory match (`annotations/exports` for new projects; the historical path remains valid for existing v0.4.0 projects).

## 10. Existing-project copy regression

Close every running LMA Studio window, then run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/regression_existing_projects.ps1
```

Expected:

- The script refuses a copy root outside system temp or without the `LMAStudioProjectRegression_*` prefix.
- It copies `Batch03Test`, `CART_Exp1-3`, `CART_Exp2-1`, and `Young_HSC3`; the EXE is never pointed at an original project.
- Every copied manifest is first confirmed to have a missing or v1 retired detector binding.
- `/api/open-project` returns HTTP 400 with a readable rebuild instruction, and the bootstrap project-selection state remains active.
- Copy and original full-tree snapshots match before and after rejection, including every relative file/directory path plus each file's SHA256, length, and UTC modification time. Directory modification times are intentionally excluded because Windows can update copied-directory metadata without changing any project file.
- The result reports `CopyStable=True`, `OriginalStable=True`, `BootstrapPreserved=True`, and `ProcessExited=True`.
- The temporary regression root is removed even after failure.

Run the optional packaged smoke only on another temporary copy of the current `Lin-_LSK` project:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/windows/regression_hsc1_packaged.ps1 `
  -ProjectDir "E:\path\to\Lin-_LSK" `
  -HscDataDir "E:\path\to\HSC1_data"
```

It verifies schema 3/layout 4, G1/G2 on one shared Green time axis, 24 min, `Off` post-run QC, Track-window APIs, the 6.002/6.017 min boundary QC candidates, the 16-column export, unchanged temporary-copy/original-project/source snapshots (`ProjectStable`, `OriginalProjectStable`, and `HscSourceStable`), and safe cleanup under `%TEMP%\LMAStudioProjectRegression_*`.

To exercise the real `Save anchor` write path, add `-ExerciseBoundaryAnchorWrite`. Both boundary relations must be accepted only in the temporary project copy; the result reports `CopyWriteIsolated=True`, while `OriginalProjectStable=True` and `HscSourceStable=True` remain required. This write exercise intentionally reports `ProjectStable=False` because the disposable copy is the authorized write target.

## 11. Package boundary

Inspect `dist/LMAStudio` and any candidate archive. They must not contain user project directories, annotation databases or exports, raw LIF/MS inputs, source/canonical event maps, project parquet tables, h5ad files, or author/manual outputs.

A manual GitHub Actions `workflow_dispatch` may upload a non-publishing test artifact. Formal GitHub Release publication is tag-triggered after the release gates pass.
