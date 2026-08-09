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

# Calling an environment's python.exe by absolute path does not activate that
# environment.  Without this isolation PyInstaller can resolve pyexpat.pyd from
# the selected environment but libexpat.dll (and other core DLLs) from the base
# Conda PATH.  Put the selected interpreter's runtime directories first.
$PythonPrefix = (& $Python -c "import sys; print(sys.prefix)").Trim()
if ($LASTEXITCODE -ne 0 -or !(Test-Path -LiteralPath $PythonPrefix)) {
    throw "Cannot resolve the selected Python environment prefix."
}
$PythonBasePrefix = (& $Python -c "import sys; print(sys.base_prefix)").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Cannot resolve the selected Python base prefix."
}
$RuntimePathDirs = @(
    $PythonPrefix,
    (Join-Path $PythonPrefix "Library\mingw-w64\bin"),
    (Join-Path $PythonPrefix "Library\usr\bin"),
    (Join-Path $PythonPrefix "Library\bin"),
    (Join-Path $PythonPrefix "Scripts"),
    (Join-Path $PythonPrefix "DLLs")
) | Where-Object { Test-Path -LiteralPath $_ }
if ($PythonBasePrefix -ne $PythonPrefix) {
    $RuntimePathDirs += @(
        $PythonBasePrefix,
        (Join-Path $PythonBasePrefix "Library\bin"),
        (Join-Path $PythonBasePrefix "DLLs")
    ) | Where-Object { Test-Path -LiteralPath $_ }
}
$env:PATH = (($RuntimePathDirs | Select-Object -Unique) + @($env:PATH)) -join [IO.Path]::PathSeparator
Write-Host "Python environment prefix: $PythonPrefix"

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

$RuntimeConfigSource = Join-Path $RepoRoot "packaging\windows\LMAStudio.exe.config"
$RuntimeConfigTarget = Join-Path $RepoRoot "dist\LMAStudio\LMAStudio.exe.config"
Copy-Item -LiteralPath $RuntimeConfigSource -Destination $RuntimeConfigTarget -Force

# The imports above prove loadability; exact hashes also prove PyInstaller did
# not silently select same-named DLLs from a different Conda environment.
$CoreRuntimeDlls = @(
    "libexpat.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "liblzma.dll",
    "libbz2.dll",
    "ffi-8.dll",
    "sqlite3.dll"
)
$PreferredDllDir = Join-Path $PythonPrefix "Library\bin"
foreach ($DllName in $CoreRuntimeDlls) {
    $ExpectedDll = Join-Path $PreferredDllDir $DllName
    if (!(Test-Path -LiteralPath $ExpectedDll)) {
        continue
    }
    $PackagedDll = Join-Path $RepoRoot "dist\LMAStudio\_internal\$DllName"
    if (!(Test-Path -LiteralPath $PackagedDll)) {
        throw "The packaged runtime is missing $DllName from the selected Python environment."
    }
    $ExpectedHash = (Get-FileHash -LiteralPath $ExpectedDll -Algorithm SHA256).Hash
    $PackagedHash = (Get-FileHash -LiteralPath $PackagedDll -Algorithm SHA256).Hash
    if ($ExpectedHash -ne $PackagedHash) {
        throw "The packaged $DllName came from a different Python/Conda environment."
    }
}

function Invoke-PackagedRuntimeProbe {
    $Probe = Start-Process -FilePath $BuiltExe -ArgumentList "--check-runtime" -WindowStyle Hidden -Wait -PassThru
    if ($Probe.ExitCode -ne 0) {
        throw "The packaged Windows desktop runtime failed its import probe (exit code $($Probe.ExitCode))."
    }
}

Invoke-PackagedRuntimeProbe

$MotwTargets = @(
    (Join-Path $RepoRoot "dist\LMAStudio\_internal\pythonnet\runtime\Python.Runtime.dll"),
    (Join-Path $RepoRoot "dist\LMAStudio\_internal\clr_loader\ffi\dlls\amd64\ClrLoader.dll")
)
foreach ($Target in $MotwTargets) {
    if (!(Test-Path -LiteralPath $Target)) {
        throw "The packaged Windows runtime is missing $Target."
    }
    Set-Content -LiteralPath $Target -Stream Zone.Identifier -Encoding Ascii -Value @(
        "[ZoneTransfer]",
        "ZoneId=3"
    )
}
try {
    Invoke-PackagedRuntimeProbe
}
finally {
    foreach ($Target in $MotwTargets) {
        Unblock-File -LiteralPath $Target
    }
}

Write-Host ""
Write-Host "Build complete: dist\LMAStudio\LMAStudio.exe"
Write-Host "Run: powershell -ExecutionPolicy Bypass -File packaging\windows\run_exe.ps1"
