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

Open:

```text
http://127.0.0.1:8050/
```

Expected:

- The page starts in initialization mode.
- No previous project is loaded automatically.
- The center of the page offers `新建项目` and `打开项目`.
- The display controls include `Y轴: 完整 / 稳健放大`.
- There is no raw RFU / corrected signal display switch.

## 3. Create Project

Click `新建项目` and fill:

- Project directory: a new empty directory.
- Three LIF raw files.
- One MS raw file.
- LIF channel names and identities.
- QC anchor pair.
- Raw input mode, preferably external reference for large MS files.

Expected:

- The project directory gets `lifms_project.json`.
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
- `后段局部校正`: use automatic MS local delta estimate, adjust delta, and freeze.
- `QC 巡检`: create a manual QC anchor with one missing LIF side if needed.
- `细胞标注`: create one manual LIF-MS760 pair and accept/reject a candidate line.
- Restart the exe and reopen the project; annotations should remain.

## 6. Export

Click `导出已接受 CSV`.

Expected:

- A CSV is downloaded.
- A copy is stored under `annotation_app/annotations/exports`.
- The filename uses project name plus timestamp.
- Pending and rejected records are not exported.

## 7. Network Warning

For local use, keep `--host 127.0.0.1`. If you intentionally expose the app on a trusted LAN:

```powershell
dist\LMAStudio\LMAStudio.exe --host 0.0.0.0 --port 8050
```

The app has no account authentication, so do not expose it to untrusted networks.
