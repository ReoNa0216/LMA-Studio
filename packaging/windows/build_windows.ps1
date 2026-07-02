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
& $Python -m pip install -r (Join-Path $RepoRoot "packaging\windows\requirements-win.txt")
& $Python -m PyInstaller --clean --noconfirm (Join-Path $RepoRoot "packaging\windows\lifms_annotation.spec")

Write-Host ""
Write-Host "Build complete: dist\LMAStudio\LMAStudio.exe"
Write-Host "Run: powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1"
