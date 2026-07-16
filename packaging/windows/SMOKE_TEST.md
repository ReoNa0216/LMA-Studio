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
- Three LIF raw files.
- One MS raw file.
- LIF channel names and identities.
- QC anchor channels: select 2 or 3 of the configured inputs, including at least one green and one red channel. The project schema supports up to 4 anchors for future input layouts.
- Raw input mode, preferably external reference for large MS files.

Expected:

- Every `选择` button opens a native Windows file/folder dialog parented to LMA Studio.
- The project directory gets `lifms_project.json`.
- `lifms_project.json` records all selected `qc_anchor_channels` and each channel's physical `time_axis`.
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

## 5. Core Workflow

On a project copy, test:

- `QC 校正`: accept/reject a candidate and create/clear one manual QC anchor.
- `QC 校正`: after at least two accepted anchors per physical axis, preview the accepted-anchor refit, verify the per-axis old/new shifts, and apply it. Applying must reset the downstream time model to draft with `MS local delta = 0`.
- After applying the QC refit, change one QC-calibration accept/reject decision. The app must require confirmation, clear the applied QC model and downstream time model, then return to the automatic QC suggestion.
- Change `QC 结束(min)` after applying the refit. The app must ask before clearing the saved QC alignment model and require a new QC refit afterward.
- `后段局部校正`: use automatic MS local delta estimate, adjust delta, and freeze.
- `QC 巡检`: create a manual QC anchor with one missing LIF side if needed.
- `细胞标注`: create one manual LIF-MS760 pair and accept/reject a candidate line.
- Restart the exe and reopen the project; annotations should remain.

## 6. Export

Click `导出已接受 CSV`.

Expected:

- A CSV is downloaded.
- The embedded WebView opens a native save dialog for the download.
- A copy is stored under `annotation_app/annotations/exports`.
- The filename uses project name plus timestamp.
- Pending and rejected records are not exported.

## 7. Desktop Lifecycle

Close and reopen `LMAStudio.exe` twice.

Expected:

- Each launch returns to the initialization page instead of reopening the previous project.
- No `LMAStudio.exe` process remains after closing.
- `%LOCALAPPDATA%\LMA Studio\logs\lma-studio.log` contains startup and shutdown records but no project data payloads.
