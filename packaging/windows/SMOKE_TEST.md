# Windows Release Smoke Test

Goal: verify that the packaged application can run without a Python setup on the user's machine and can create, open, annotate, restart, and export projects.

## 1. Build

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build_windows.ps1
```

Expected output:

```text
dist\LMAStudio\LMAStudio.exe
```

## 2. First Startup

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1
```

Expected:

- A native window titled `LMA Studio` opens without an external browser or console window.
- The page starts in initialization mode.
- No previous project is loaded automatically.
- The center of the page offers `新建项目` and `打开项目`.
- The display controls include `Y轴: 完整 / 稳健放大`.
- There is no raw RFU / corrected signal display switch.
- A second launch reports that LMA Studio is already running.
- Closing the window terminates the process and releases its random loopback port.

## 3. Create Project

Click `新建项目` and fill:

- Project directory: a new empty directory.
- Two to four LIF raw files.
- Use `+ 添加 LIF` / remove controls to verify both 2- and 4-input layouts.
- One MS raw file.
- One event-coordinate CSV containing `scan_start_time`, `UMAP1`, and `UMAP2`.
- LIF channel names and identities.
- Per-channel QC and cell roles. Exercise QC-only, cell-only, both, and disabled roles.
- QC anchor channels: select 2-4 configured inputs, cover every cell-annotation time axis, and include at least one green and one red detector.
- Raw input mode, preferably external reference for large MS files.

For the four-channel acceptance layout, add a fourth LIF row and configure
`G1/G2/R1/R2`. A useful role test is QC=`G1/G2/R1` and
cell=`G1/G2/R2`: this makes R1 QC-only and R2 cell-only while both physical
time axes remain covered.

Expected:

- Every `选择` button opens a native Windows file/folder dialog parented to LMA Studio.
- The project directory gets `lifms_project.json`.
- `lifms_project.json` records all selected `qc_anchor_channels` and each channel's physical `time_axis`.
- `lifms_project.json` records schema 2/layout 3, every channel's `use_for_cell_annotation`, and the canonical event-map SHA/source SHA/count.
- `data/interim/lma/cell_event_umap.csv` contains exactly `ms_event_id,scan_id,scan_start_time,UMAP1,UMAP2`; it contains no source `Type/leiden/CellNumber`.
- A complete but conflicting or cross-axis-incoherent QC group is visible for review but is excluded from whole-window batch acceptance.
- Reopen a reviewed project and confirm an accepted QC relation is rendered once. The same relation must not return as pending under a different auto/manual ID, and candidates that reuse an accepted MS event or LIF peak must not enter the pending count.
- Intermediate parquet tables are generated under `data/interim/v3`.
- `annotation_app/annotations/annotation.sqlite` is created.
- The UI enters the synchronized track view.

## 4. Open Existing Project

Click `打开项目` and choose an existing project directory.

Expected:

- If `lifms_project.json` exists, manifest fingerprints are checked first.
- The SQLite database is validated against current parquet peak/event IDs.
- Valid projects open without auto-requiring raw inputs.
- A schema-v1 project without a map opens with the legacy unfiltered workflow and shows `UMAP（未配置）`; opening it does not change manifest/SQLite/parquet hashes.
- Clicking `UMAP（未配置）` opens the configuration dialog with a readable explanation instead of silently doing nothing.
- The attach-map path field occupies the available dialog width, shows the selected path, and keeps the validation/write action disabled until a CSV is selected.
- The explicit attach-map action refuses a CSV that omits an already accepted post-start event, and a successful attach preserves all annotations and time models.

## 5. Core Workflow

On a project copy, test:

- `QC 校正`: accept/reject a candidate and create/clear one manual QC anchor.
- `QC 校正`: after at least two accepted anchors per physical axis, preview the accepted-anchor refit, verify the per-axis old/new shifts, and apply it. Applying must reset the downstream time model to draft with `MS local delta = 0`.
- After applying the QC refit, change one QC-calibration accept/reject decision. The app must require confirmation, clear the applied QC model and downstream time model, then return to the automatic QC suggestion.
- Change `QC 结束(min)` after applying the refit. The app must ask before clearing the saved QC alignment model and require a new QC refit afterward.
- `后段局部校正`: use automatic MS local delta estimate, adjust delta, and freeze.
- `事件标注`: switch `全部 / QC / 细胞`, create a manual QC anchor with a missing LIF side, then create one manual LIF-MS760 cell pair.
- Verify an out-of-map MS event is passive in the UI and rejected by the backend even if its ID is submitted directly.
- Accepting QC then trying to accept cell on the same MS event (and the reverse order) must be rejected until the first relation is revoked.
- The third stage has no whole-window batch-accept action.
- Restart the exe and reopen the project; annotations should remain.

## 6. UMAP Window

On a project with a map:

- Click `打开 UMAP`; a separate resizable native window opens, while the main synchronized-track window remains unchanged.
- Repeated clicks restore the same UMAP window rather than creating duplicates.
- The window initially shows all points centered. Maximizing/restoring the window automatically refits the cloud to the new plot area.
- The visible `UMAP1` / `UMAP2` axes and tick values update with wheel zoom and drag pan.
- `显示全部点` restores all points to a centered, readable scale without changing annotations; the on-canvas hint explains wheel zoom, drag pan, and click-to-locate.
- Hover shows only the MS760 time and current human-readable label; it does not expose internal event/scan IDs.
- Pan, wheel-zoom, reset, hover, resize, and high-DPI rendering remain responsive.
- Unknown points are gray, accepted QC is black, accepted cells use the corresponding LIF channel color, and conflicts have an explicit red outline/X.
- Accept/revoke in the main window updates UMAP without reloading either window.
- Clicking a UMAP point switches the main window to event annotation and centers the matching `ms_event_id`.
- Clicking a UMAP point uses the containing 2.5 min grid window rather than centering on an arbitrary decimal start; for example, an event at `50.075 min` opens `50.0-52.5 min`.
- Before the local delta is frozen, third-stage candidates/manual tools remain unavailable and direct backend acceptance is rejected. Applying the optional accepted-anchor QC refit is not a prerequisite; either the automatic QC base alignment or an applied refit may feed the frozen local model.
- Closing/reopening only the UMAP window does not change SQLite or close the main window. Closing the main window closes the UMAP window.
- Open a different project and confirm the UMAP window clears the prior project/map state before drawing the new one.

## 7. Export

Click `导出已接受 CSV`.

Expected:

- A CSV is downloaded.
- The embedded WebView opens a native save dialog for the download.
- A copy is stored under `annotation_app/annotations/exports`.
- The filename uses project name plus timestamp.
- Pending and rejected records are not exported.
- Third-stage rows contain `UMAP1`, `UMAP2`, and `cell_event_map_sha256`; early QC rows leave them blank.
- No source `Type`, `leiden`, or `CellNumber` column is exported.

## 8. Desktop Lifecycle

Close and reopen `LMAStudio.exe` twice.

Expected:

- Each launch returns to the initialization page instead of reopening the previous project.
- No `LMAStudio.exe` process remains after closing.
- `%LOCALAPPDATA%\LMA Studio\logs\lma-studio.log` contains startup and shutdown records but no project data payloads.

## 9. Package Boundary

Inspect the final `dist\LMAStudio` directory and archive. It must not contain
any user project directory, raw LIF/MS input, source/canonical UMAP CSV,
`annotation.sqlite`, parquet table, exported annotation CSV, or h5ad file.
