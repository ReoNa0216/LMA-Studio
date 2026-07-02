# LMA Studio v0.1.0 Release Notes

## Package

- Product: LMA Studio
- Meaning: LIF-MS Annotation Studio
- Platform: Windows x64
- Build type: PyInstaller onedir
- Entry point: `LMAStudio\LMAStudio.exe`
- Local URL: `http://127.0.0.1:8050/`

## What Is Included

- Local project initialization page with `新建项目` and `打开项目`.
- Project-based workflow with `lifms_project.json`.
- Configurable 3-channel LIF layout and configurable QC anchor pair.
- External raw input reference mode for large MS files.
- Four annotation workflow stages:
  - QC calibration
  - Local post-QC MS shift calibration
  - QC survey
  - Cell annotation
- QC-pair-based local MS delta suggestion.
- Accepted annotation CSV export using project name plus timestamp.

## What Is Not Included

- No bundled user data.
- No raw LIF/MS files.
- No project SQLite annotation databases.
- No author CSV or h5ad inputs.
- No WebView wrapper yet; v0.1.0 opens the local browser intentionally for transparent debugging.

## Install And Run

1. Download `LMA-Studio-v0.1.0-windows-x64.zip`.
2. Unzip it to a writable directory.
3. Double-click `LMAStudio.exe`.
4. Open `http://127.0.0.1:8050/` if the browser does not open automatically.
5. Choose `新建项目` or `打开项目`.

## Known Notes

- Keep the whole `LMAStudio` folder together. The single exe depends on bundled runtime files in the same folder.
- If port `8050` is already in use, launch from PowerShell with another port:

```powershell
.\LMAStudio.exe --port 8051
```

- For large MS raw text files, use external raw input references unless a self-contained project copy is required.

## Validation Before Release

Validated on local Windows environment:

- `python -m unittest discover -s tests`
- Windows PyInstaller build
- Startup returns initialization page with no remembered project
- Project open smoke tests for:
  - `CART_Exp1-3`
  - `CART_Exp2-1`
  - `Batch03Test`
