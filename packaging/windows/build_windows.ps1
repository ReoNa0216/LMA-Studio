param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$VenvDir = Join-Path $RepoRoot ".venv-win"

Set-Location $RepoRoot

if (!$PythonExe -and $env:LIFMS_BUILD_PYTHON) {
    $PythonExe = $env:LIFMS_BUILD_PYTHON
}

if (!$PythonExe) {
    $Conda = Get-Command conda -ErrorAction SilentlyContinue
    if ($Conda) {
        $CondaEnvs = (& conda env list --json | ConvertFrom-Json).envs
        $CondaEnv = $CondaEnvs | Where-Object { $_ -match "[\\/]lifms_annotation_win$" } | Select-Object -First 1
        if ($CondaEnv) {
            $Candidate = Join-Path $CondaEnv "python.exe"
            if (Test-Path $Candidate) {
                $PythonExe = $Candidate
            }
        }
    }
}

if (!$PythonExe) {
    $PythonExe = Join-Path $VenvDir "Scripts\python.exe"
    if (!(Test-Path $PythonExe)) {
        py -3.11 -m venv $VenvDir
    }
}

$Python = (Resolve-Path $PythonExe).Path

Write-Host "Using Python: $Python"

& $Python -m pip install --upgrade pip wheel setuptools
if ($LASTEXITCODE -ne 0) {
    throw "Failed to update the Windows build toolchain (exit code $LASTEXITCODE)."
}
& $Python -m pip install -r (Join-Path $RepoRoot "packaging\windows\requirements-win.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install Windows build requirements (exit code $LASTEXITCODE)."
}
& $Python -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed; refusing to create a Windows package (exit code $LASTEXITCODE)."
}
& $Python (Join-Path $RepoRoot "packaging\windows\generate_icon.py")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to generate the Windows application icon (exit code $LASTEXITCODE)."
}
& $Python -m PyInstaller --clean --noconfirm (Join-Path $RepoRoot "packaging\windows\lifms_annotation.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed (exit code $LASTEXITCODE)."
}

$BuiltExe = Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe"
if (!(Test-Path $BuiltExe)) {
    throw "PyInstaller returned success but did not create $BuiltExe."
}

Write-Host ""
Write-Host "Build complete: dist\LMAStudio\LMAStudio.exe"
Write-Host "Run: powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1"
